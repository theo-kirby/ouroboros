"""Opt-in topology evaluation adapter; the production Engine is unchanged.

Run with python -m ouroboros.experiment. Control files must be outside research
workspaces. Working-directory separation is NOT a filesystem security boundary;
the launcher must confine untrusted/live agents before benchmark admission.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import json
import math
import os
import resource
from pathlib import Path
import signal
import sys
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
import yaml

from . import publication, view
from .config import PROFILE_TEMPLATE, FiguresConfig, PublicationConfig
from .engine import role_env
from .gitguard import GitGuard
from .harness import make_harness
from .harness.base import Result
from .harness.backend_headless import kill_active, COMMAND_PREFIX
from .memory import ENV, make_memory
from .memory.graph import skill
from .recorder import Recorder


class ExperimentFigures(FiguresConfig):
    model_config = ConfigDict(extra='forbid', frozen=True)


class ExperimentPublication(PublicationConfig):
    """Production's publication keys (DESIGN.md section 23). Here consolidate_every counts
    cycles, and the final pass is folded into the closing writer calls, never an extra call."""
    model_config = ConfigDict(extra='forbid', frozen=True)
    figures: ExperimentFigures = Field(default_factory=ExperimentFigures)


# Who keeps the publication, and whose profile guidance each identity gets.
GENERALISTS = ('researcher','peer_a','peer_b','solver_a','solver_b')
WRITERS = ('scribe',)+GENERALISTS
PROFILE_ROLES = {'actor':('actor',),'critic':('critic',),'planner':('critic',),
                 **{g:('actor','critic') for g in GENERALISTS}}


class ExperimentConfig(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True)
    run: str = 'experiment'
    topology: Literal['standard','planner','freeflow','peers','blind'] = 'standard'
    harness: str = 'codex'
    model: str
    memory: Literal['files','governor'] = 'files'
    after_seconds: float = Field(default=7200, gt=0, allow_inf_nan=False)
    timeout_seconds: float = Field(default=1200, gt=0, allow_inf_nan=False)
    max_calls: int = Field(default=60, gt=0)
    max_cycles: int = Field(default=20, gt=0)
    max_tokens: int | None = Field(default=None, gt=0)
    max_cost_usd: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    max_failures: int = Field(default=3, gt=0)
    blind_calls: int = Field(default=1, gt=0)
    guidance: dict[str,str] = Field(default_factory=dict)
    confinement: Literal['landlock','none'] = 'landlock'
    read_paths: tuple[str,...] = ()
    input_manifest: str | None = None
    memory_bytes: int = Field(default=8*1024**3, gt=0)
    max_cpu_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    profile: Literal['research','development','freeform'] = 'freeform'
    publication: ExperimentPublication = Field(default_factory=ExperimentPublication)

    @property
    def template(self) -> str:
        t=self.publication.template
        return PROFILE_TEMPLATE[self.profile] if t=='auto' else t

    @model_validator(mode='after')
    def known_guidance(self):
        names={'actor','critic','scribe','planner','researcher','peer_a','peer_b','solver_a','solver_b'}
        if self.guidance.keys()-names:
            raise ValueError('unknown guidance identity')
        if not self.run or Path(self.run).name != self.run or self.run in ('.','..'):
            raise ValueError('run must be a single path component')
        return self


SCHEMA={'type':'object','properties':{'report':{'type':'string'},'handoff':{'type':'string'},
                                   'done':{'type':'boolean'}},'required':['report','handoff','done'],
        'additionalProperties':False}


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def atomic(path: Path, value: dict) -> None:
    tmp=path.with_suffix(path.suffix+'.tmp')
    with tmp.open('w') as f:
        json.dump(value,f,indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    os.replace(tmp,path)
    fd=os.open(path.parent,os.O_RDONLY)
    try: os.fsync(fd)
    finally: os.close(fd)


@contextmanager
def environment(values):
    before={k:os.environ.get(k) for k in values}
    os.environ.update(values)
    try: yield
    finally:
        for k,v in before.items():
            if v is None: os.environ.pop(k,None)
            else: os.environ[k]=v


class ExperimentRunner:
    def __init__(self, config: ExperimentConfig, repo: Path, control: Path, *, harness=None,
                 private_workspaces: dict[str,Path] | None=None, now=time.time):
        self.config=config; self.repo=repo.resolve(); self.control=control.resolve(); self.now=now
        self.workspaces={'shared':self.repo,**{k:v.resolve() for k,v in (private_workspaces or {}).items()}}
        if config.topology=='blind' and set(self.workspaces)!= {'shared','solver_a','solver_b'}:
            raise ValueError('blind topology requires solver_a and solver_b private workspaces')
        roots=list(self.workspaces.values())
        if len(set(roots))!=len(roots) or any(a.is_relative_to(b) for a in roots for b in roots if a!=b):
            raise ValueError('workspaces must be distinct and non-nested')
        if any(self.control.is_relative_to(p) or p.is_relative_to(self.control) for p in roots):
            raise ValueError('control directory must be outside every research workspace')
        self.goal=(self.repo/'.ouroboros/goal.md').read_text()
        for root in roots:
            if (root/'.ouroboros/goal.md').read_text()!=self.goal:
                raise ValueError('all workspaces must have the same frozen charter')
        self.inputs=self._load_inputs()
        self.template=skill('ouroboros-experiment')
        manifest={'config':config.model_dump(),'goal':self.goal,'template':self.template,'inputs':self.inputs,
                  'skills':{n:skill(n) for n in ('ouroboros-profile','ouroboros-publish')},
                  'publication_sha256':hashlib.sha256(Path(__file__).with_name('publication.py').read_bytes()).hexdigest(),
                  'workspaces':{k:str(v) for k,v in self.workspaces.items()},
                  'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                  'sandbox_sha256':hashlib.sha256(Path(__file__).with_name('experiment_sandbox.py').read_bytes()).hexdigest(),
                  'backend_sha256':hashlib.sha256(Path(__file__).parent.joinpath('harness/backend_headless.py').read_bytes()).hexdigest()}
        self.fingerprint=digest(json.dumps(manifest,sort_keys=True))
        self.control.mkdir(parents=True,exist_ok=True)
        self.checkpoint=self.control/'checkpoint.json'
        self.harness=harness or make_harness(config.harness)
        self.recorder=Recorder(self.control,echo=None)
        self.memories={k:make_memory(config.memory,p) for k,p in self.workspaces.items()}
        self.guards={k:GitGuard(p,GitGuard(p,'').current_branch()) for k,p in self.workspaces.items()}
        with (self.control/'runner.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            if self.checkpoint.exists():
                self.state=json.loads(self.checkpoint.read_text())
                if self.state['fingerprint']!=self.fingerprint:
                    raise ValueError('resume fingerprint differs: configuration, charter, template, code or workspace changed')
            else:
                if any(g.is_dirty() for g in self.guards.values()):
                    raise ValueError('new experiment requires clean prepared workspaces; preserve historical work')
                self.state=dict(fingerprint=self.fingerprint,started_at=self.now(),calls=0,tokens=0,cost_usd=0.,
                                cursor=0,cycle=0,cpu_seconds=0.,sessions={},handoff='',reports={},receipts=[],failures=0,
                                initial_heads={k:g.head() for k,g in self.guards.items()},unknown_token_calls=0,unknown_cost_calls=0,
                                pending=None,stop_reason=None,done_pending=False)
                atomic(self.control/'manifest.json',manifest)
                for key,memory in self.memories.items():
                    memory.start(run=config.run,goal_text=self.goal,run_dir=self.control,branch=self.guards[key].branch)
                    self.guards[key].commit(f'experiment: initialize {config.run}')
                self.save()
            self.schedule=self._schedule()
            self.recover()

    def _load_inputs(self):
        if self.config.input_manifest is None:return {}
        name=self.config.input_manifest
        if Path(name).is_absolute() or '..' in Path(name).parts:
            raise ValueError('input_manifest must be workspace-relative')
        raw=(self.repo/name).read_bytes()
        manifest=json.loads(raw)
        entries=manifest.get('files')
        if entries is None:entries=list(manifest['inputs'].values())+manifest.get('masks',[])
        for entry in entries:
            if not {'path','sha256','bytes'}<=entry.keys():raise ValueError('input hash/size/path required')
            if Path(entry['path']).is_absolute() or '..' in Path(entry['path']).parts:
                raise ValueError('input paths must be workspace-relative')
        result={'path':name,'sha256':hashlib.sha256(raw).hexdigest(),'entries':entries}
        self._check_inputs(result)
        return result

    def _check_inputs(self, inputs):
        if not inputs:return
        for workspace in self.workspaces.values():
            if hashlib.sha256((workspace/inputs['path']).read_bytes()).hexdigest()!=inputs['sha256']:
                raise ValueError('input manifest changed or differs between workspaces')
            for entry in inputs['entries']:
                path=workspace/entry['path']
                if path.is_symlink() or not path.resolve().is_relative_to(workspace):
                    raise ValueError('input cannot escape workspace through a symlink')
                if path.stat().st_size!=entry['bytes']:
                    raise ValueError('input size mismatch')
                with path.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                if actual!=entry['sha256']:raise ValueError('input hash mismatch')

    def _schedule(self):
        if self.config.topology=='freeflow': return ['researcher']
        if self.config.topology=='peers': return ['peer_a','peer_b']
        if self.config.topology=='planner': return ['planner','actor','critic','scribe']
        return ['actor','critic','scribe']

    def save(self): atomic(self.checkpoint,self.state)

    def recover(self):
        pending=self.state.get('pending')
        if not pending: return
        path=self.control/'receipts'/f"{pending['call']:05d}.json"
        if path.exists():
            self._apply(json.loads(path.read_text()))
        else:
            # The process may have performed work/spent tokens. Never charge zero,
            # restart its identity, or repeat the call automatically.
            self.state['stop_reason']='interrupted_inflight'; self.save()

    def _stage(self):
        if self.config.topology=='blind' and self.state['cursor']<2*self.config.blind_calls:
            return 'solver_a' if self.state['cursor']<self.config.blind_calls else 'solver_b'
        offset=2*self.config.blind_calls if self.config.topology=='blind' else 0
        return self.schedule[(self.state['cursor']-offset)%len(self.schedule)]

    def _workspace(self,agent):
        return agent if agent.startswith('solver_') else 'shared'

    def _persistent(self,agent):
        return agent in ('researcher','peer_a','peer_b','solver_a','solver_b')

    def _limit(self):
        s=self.state;c=self.config
        if s['stop_reason']: return s['stop_reason']
        if self.now()-s['started_at']>=c.after_seconds:return 'deadline'
        if s['calls']>=c.max_calls:return 'max_calls'
        if c.max_cpu_seconds is not None and s['cpu_seconds']>=c.max_cpu_seconds:return 'max_cpu_seconds'
        if c.max_tokens is not None and s['tokens']>=c.max_tokens:return 'max_tokens'
        if c.max_cost_usd is not None and s['cost_usd']>=c.max_cost_usd:return 'max_cost_usd'
        if s['cycle']>=c.max_cycles:return 'max_cycles'
        return None

    def _prompt(self,agent,key):
        readonly=agent in ('planner','critic')
        writer=agent in ('scribe','researcher','peer_a','peer_b','solver_a','solver_b')
        authority=('Read-only tools and memory: ouroboros mem orient/show/check. Do not edit files.' if readonly
                   else 'You are the sole active memory writer; use ouroboros mem record/fold/check/render.' if writer
                   else 'Work on code/data/report; memory is read-only through ouroboros mem orient/show. The scribe records.')
        responsibility={'planner':'plan the next actor turn','actor':'execute one research unit',
                        'critic':'independently review evidence and decide completion',
                        'scribe':'record the critic judgment and maintain memory/publication',
                        'researcher':'all research responsibilities, including self-review'}.get(agent,
                        'all research responsibilities with no permanent specialized role')
        # Blind first passes receive no shared or other solver reports/handoffs.
        context=({'own_report':self.state['reports'].get(agent,'')} if key!='shared' else
                 {'handoff':self.state['handoff'],'reports':self.state['reports'],'blind_exports':self.state.get('blind_exports',{})})
        values={'identity':agent,'responsibility':responsibility,'authority':authority,'goal':self.goal,
                'memory':self.memories[key].orient_prompt(),'context':json.dumps(context),
                'budget':json.dumps({'seconds':max(0,self.config.after_seconds-(self.now()-self.state['started_at'])),
                                     'calls':self.config.max_calls-self.state['calls'],
                                     'tokens_used':self.state['tokens'],'cost_usd_equivalent':self.state['cost_usd']}),
                'story_path':str(view.story_path(self.workspaces[key]/'.ouroboros/runs'/self.config.run)),
                'summary_path':str(view.summary_path(self.workspaces[key]/'.ouroboros/runs'/self.config.run)),
                'guidance':self.config.guidance.get(agent,'(baseline guidance)'),
                'profile':publication.profile_text(self.config.profile,*PROFILE_ROLES.get(agent,())),
                'publication':self._publication(agent,key)}
        # Single substitution pass: charter/report braces must never be treated as template slots.
        import re
        return re.sub(r'\{([a-z_]+)\}',lambda m:values.get(m[1],m[0]),self.template)

    def _view(self,key):
        return view.view_dir(self.workspaces[key]/'.ouroboros/runs'/self.config.run)

    def _closing(self):
        """The last stretch of the shared budget, or the scribe call after a critic's done:
        the final consolidation happens inside these calls, so no arm gets an extra one."""
        s=self.state;c=self.config
        left=c.after_seconds-(self.now()-s['started_at'])
        return (s['done_pending'] or c.max_calls-s['calls']<=len(self.schedule)
                or c.max_cycles-s['cycle']<=1 or left<=2*c.timeout_seconds)

    def _publish_due(self,agent):
        c=self.config
        if c.template=='notebook' or agent not in WRITERS:return False
        every=c.publication.consolidate_every
        if every and self.state['cycle']-self.state.get('published_cycle',0)>=every:return True
        return c.publication.final and self._closing()

    def _publication(self,agent,key):
        """The report rules for this call. Empty for a notebook, so a control arm's prompt is unchanged."""
        c=self.config
        if c.template=='notebook':return ''
        if agent not in WRITERS:
            return ('The publication is kept by the memory writer: save each plot, screenshot or table you make '
                    'in the workspace with the script and data behind it, and name the files in your report.')
        parts=[publication.story_rules(c)]
        if self._publish_due(agent):
            parts.append(publication.pass_rules(c))
        elif agent in GENERALISTS and c.publication.final:
            parts.append('If you will set done to true in this turn, first rewrite the publication as a whole:\n\n'
                         +publication.pass_rules(c))
        if self.state.get('publication_problem'):
            parts.append('Fix these in the publication first.\n'+self.state['publication_problem'])
        vd=self._view(key)
        return publication.fill('\n\n'.join(parts),view_dir=str(vd),story_path=str(vd/'story.html'),
                                assets_dir=str(vd/'assets'),summary_path=str(vd/'summary.txt'))

    def _check_publication(self,receipt):
        """Deterministic, host-side: no model call. Findings reach the next writer."""
        s=self.state;agent=receipt['agent']
        if self.config.template=='notebook' or agent not in WRITERS:return
        due=receipt.get('publish_due')
        if due and receipt['ok']:s['published_cycle']=s['cycle']
        if not (due or s.get('publication_problem')):return
        try:problems=publication.check(self.config,self._view(receipt['workspace']))
        except Exception as exc:
            self.recorder.log(f'publication check raised {exc!r}');return
        s['publication_problem']=publication.describe(problems)
        self.recorder.step(iteration=s['cycle'],step='publish',call=receipt['call'],consolidated=bool(due) or None,
                           problems=len(problems),problem=(s['publication_problem'] or '')[:300] or None)

    def _freeze_solver(self, agent, key):
        """Preserve first-pass changed files for synthesis; inputs stay in the packet.

        This prevents accidental loss/rewriting, not malicious same-user access.
        External filesystem confinement remains a campaign launch prerequisite.
        """
        import shutil
        guard=self.guards[key]
        base=self.state['initial_heads'][key]
        names=set(guard.changed_files(base))
        names.update(guard.git('ls-files','--others','--exclude-standard').splitlines())
        source_view=view.view_dir(self.workspaces[key]/'.ouroboros/runs'/self.config.run)
        if source_view.exists():
            names.update(str(p.relative_to(self.workspaces[key])) for p in source_view.rglob('*') if p.is_file())
        target=self.repo/'evidence/blind-first-pass'/agent
        target.mkdir(parents=True,exist_ok=True)
        manifest=[]
        for name in sorted(names):
            source=self.workspaces[key]/name
            if source.is_symlink() or not source.resolve().is_relative_to(self.workspaces[key]):
                raise ValueError('blind export cannot follow symlinks outside its workspace')
            if not source.is_file():continue
            dest=target/name;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest)
            manifest.append({'path':name,'bytes':source.stat().st_size,
                             'sha256':hashlib.sha256(source.read_bytes()).hexdigest()})
        atomic(self.control/f'{agent}-snapshot.json',{'files':manifest})
        self.state.setdefault('blind_exports',{})[agent]={'path':str(target),'files':manifest}

    @contextmanager
    def _confinement(self, agent, key):
        """Credential-free CLI/tool confinement. Never expose host auth or history.

        Authenticated model execution needs an independently provisioned credential
        boundary/broker; this helper does not copy, link or discover credentials.
        """
        if self.config.confinement=='none':
            self.state['pending']['confinement']={'kind':'none'};self.save()
            yield
            return
        from .experiment_sandbox import abi
        if abi()<3:raise ValueError('Landlock ABI3+ required; no unrestricted fallback')
        home=self.control/'agent-homes'/agent
        home.mkdir(parents=True,exist_ok=True)
        temporary=home/'tmp';temporary.mkdir(exist_ok=True)
        reads=[Path(p).resolve() for p in ('/usr','/bin','/lib','/lib64','/etc') if Path(p).exists()]
        reads += [Path(sys.prefix).resolve(),Path(__file__).parent.resolve()]
        reads += [Path(p).resolve() for p in self.config.read_paths]
        reads += [Path(p) for p in ('/dev/null','/dev/urandom','/dev/random','/proc/cpuinfo','/proc/meminfo') if Path(p).exists()]
        binary=__import__('shutil').which(getattr(self.harness,'binary',''))
        if binary:reads.append(Path(binary).resolve())
        forbidden=[self.control]+[p for k,p in self.workspaces.items() if k!=key]
        for read in reads:
            if any(read.is_relative_to(p) or p.is_relative_to(read) for p in forbidden):
                raise ValueError('read allowlist overlaps private control/peer workspace')
        prefix=[sys.executable,'-I',str(Path(__file__).with_name('experiment_sandbox.py')),
                '--cpu',str(min(os.sched_getaffinity(0))),'--memory-bytes',str(self.config.memory_bytes)]
        for read in reads:prefix += ['--read',str(read)]
        prefix += ['--read' if agent in ('critic','planner') else '--write',str(self.workspaces[key]),
                   '--write',str(home),'--']
        self.state['pending']['confinement']={'kind':'landlock','abi':abi(),
            'read_paths':[str(p) for p in reads],
            'workspace_access':'readonly' if agent in ('critic','planner') else 'readwrite',
            'workspace':str(self.workspaces[key]),'identity_home':str(home),
            'cpu_affinity':[min(os.sched_getaffinity(0))],'address_limit_bytes':self.config.memory_bytes}
        self.save()
        token=COMMAND_PREFIX.set(tuple(prefix))
        try:
            with environment({'HOME':str(home),'CODEX_HOME':str(home/'.codex'),'TMPDIR':str(temporary),
                              'OPENBLAS_NUM_THREADS':'1','OMP_NUM_THREADS':'1','MKL_NUM_THREADS':'1'}):yield
        finally:COMMAND_PREFIX.reset(token)

    def step(self):
        if self._limit():return
        if any((p/'.ouroboros/goal.md').read_text()!=self.goal for p in self.workspaces.values()):
            self.state['stop_reason']='charter_modified';self.save();return
        try:self._check_inputs(self.inputs)
        except (OSError,ValueError):
            self.state['stop_reason']='input_modified';self.save();return
        agent=self._stage();key=self._workspace(agent);call=self.state['calls']+1
        prompt=self._prompt(agent,key)
        pending={'call':call,'agent':agent,'workspace':key,'started_at':self.now(),'publish_due':self._publish_due(agent),
                 'head_before':self.guards[key].head(),'prompt_sha256':digest(prompt),'resume':self.state['sessions'].get(agent) if self._persistent(agent) else None}
        self.state['pending']=pending;self.save()
        self.recorder.transcript_path(call,agent).with_suffix('.prompt.txt').write_text(prompt)
        principal='critic' if agent in ('critic','planner') else 'actor' if agent=='actor' else 'scribe'
        self.publish(agent)
        timeout=min(self.config.timeout_seconds,max(0,self.config.after_seconds-(self.now()-self.state['started_at'])))
        if timeout<=0:
            self.state['pending']=None;self.state['stop_reason']='deadline';self.save();return
        start=self.now()
        usage_before=resource.getrusage(resource.RUSAGE_CHILDREN)
        try:
            with role_env(principal), environment({ENV:self.config.memory,'OUROBOROS_EXPERIMENT_AGENT':agent}), self._confinement(agent,key):
                result=self.harness.run(prompt,cwd=self.workspaces[key],timeout=timeout,
                    resume=pending['resume'],model=self.config.model,json_schema=SCHEMA,
                    tools='readonly' if agent in ('critic','planner') else None,
                    log_path=self.recorder.transcript_path(call,agent))
        except (KeyboardInterrupt,SystemExit):
            kill_active();self.state['stop_reason']='interrupted_inflight';self.save();raise
        except Exception as exc:
            result=Result(exit_code=-1,error=f'{type(exc).__name__}: {exc}')
        usage_after=resource.getrusage(resource.RUSAGE_CHILDREN)
        receipt={**pending,
                 'cpu_seconds':max(0,(usage_after.ru_utime+usage_after.ru_stime)-(usage_before.ru_utime+usage_before.ru_stime)),
                 'child_high_water_rss_kib':usage_after.ru_maxrss,
                 'head_after_agent':self.guards[key].head(),'elapsed_s':max(0,self.now()-start),'text':result.text,'ok':result.ok,
                 'session_id':result.session_id,'cost_usd':result.cost_usd,'tokens':result.tokens,
                 'model':result.model,'error':result.error,'timed_out':result.timed_out}
        (self.control/'receipts').mkdir(exist_ok=True)
        atomic(self.control/'receipts'/f'{call:05d}.json',receipt)
        self._apply(receipt)
        self.publish('idle')

    def _apply(self,receipt):
        if receipt['call']<=self.state['calls']:return
        s=self.state;agent=receipt['agent'];key=receipt['workspace']
        s['calls']+=1;s['pending']=None;s['receipts'].append(receipt)
        s['cpu_seconds']+=receipt['cpu_seconds']
        tokens=receipt['tokens'];cost=receipt['cost_usd']
        valid_tokens=isinstance(tokens,dict) and all(type(n) is int and n>=0 for n in tokens.values())
        valid_cost=type(cost) in (float,int) and math.isfinite(cost) and cost>=0
        if valid_tokens:s['tokens']+=sum(tokens.values())
        if valid_cost:s['cost_usd']+=cost
        if not valid_tokens:s['unknown_token_calls']+=1
        if not valid_cost:s['unknown_cost_calls']+=1
        if (self.config.max_tokens is not None and not valid_tokens) or (self.config.max_cost_usd is not None and not valid_cost):
            s['stop_reason']='meter_unavailable'
        if receipt['model'] and receipt['model']!=self.config.model:
            s['stop_reason']='model_changed'
        session=receipt['session_id']
        if self._persistent(agent):
            previous=s['sessions'].get(agent)
            other_sessions={v for k,v in s['sessions'].items() if k!=agent}
            if (receipt['ok'] and not session) or (session and previous and session!=previous) or (session and session in other_sessions):
                s['stop_reason']='session_unavailable'
        if session:s['sessions'][agent]=session
        response=None
        try:
            response=json.loads(receipt['text'])
            if set(response)!=set(SCHEMA['required']) or type(response['done']) is not bool or not all(isinstance(response[k],str) for k in ('report','handoff')):
                response=None
        except (ValueError,TypeError):response=None
        if receipt['ok'] and response:
            s['failures']=0;s['reports'][agent]=response['report']
            if key=='shared':s['handoff']=response['handoff']
            if agent=='critic':s['done_pending']=response['done']
            if agent in ('researcher','peer_a','peer_b') and response['done']:
                s['done_source']='self_review' if agent=='researcher' else 'peer_claim'
                s['stop_reason']=s['stop_reason'] or 'done_claimed'
            if agent=='scribe' and s['done_pending']:
                s['done_source']='critic_reported';s['stop_reason']=s['stop_reason'] or 'done_claimed'
            if agent.startswith('solver_') and (s['cursor']+1)%self.config.blind_calls==0:
                self._freeze_solver(agent,key)
            s['cursor']+=1
            if agent in ('scribe','researcher','peer_a','peer_b'):s['cycle']+=1
            self._check_publication(receipt)
        else:
            s['failures']+=1
            if key=='shared':s['handoff']=f"Previous {agent} call failed or returned invalid JSON; recover. {receipt['error'] or ''}"
            if s['failures']>=self.config.max_failures:s['stop_reason']=s['stop_reason'] or 'failure_limit'
        # A missing memory record never becomes an invented scientific acceptance.
        # The deterministic host journal retains the full call even if the writer failed.
        self.save()
        self.recorder.step(iteration=s['cycle'],step=agent,call=s['calls'],exit=0 if receipt['ok'] else 1,
                           cost=cost,tokens=sum(tokens.values()) if valid_tokens else None,session=session)
        if agent=='critic' and receipt['ok'] and response:
            self.recorder.decision(iteration=s['cycle']+1,verdict='done_accepted' if response['done'] else 'continue',
                                   reason=response['report'],reply=response['handoff'],source='experimental_critic',
                                   verification='not_reported',verification_evidence='External evaluation remains separate.')
        if agent not in ('critic','planner'):
            try:self.guards[key].commit(f'experiment {self.config.run} call{s["calls"]}: {agent}')
            except Exception as exc:
                s['stop_reason']='checkpoint_git_error';s['checkpoint_error']=repr(exc);self.save()

    def publish(self,state):
        s=self.state
        self.recorder.status(run=self.config.run,state=state,topology=self.config.topology,
                             iteration=s['cycle'],calls=s['calls'],tokens_total=s['tokens'],cpu_seconds=s['cpu_seconds'],
                             cost_usd=s['cost_usd'],unknown_token_calls=s['unknown_token_calls'],
                             unknown_cost_calls=s['unknown_cost_calls'],elapsed=self.now()-s['started_at'],
                             stop_reason=s['stop_reason'],done_source=s.get('done_source'),
                             time_budget=self.config.after_seconds,pid=os.getpid())
        self._publish_authored_files()
        view.render(self.control,self.repo,goal_text=self.goal,graph=self.memories['shared'].graph(),alive=state!='stopped')

    def _publish_authored_files(self):
        import shutil
        source=view.view_dir(self.repo/'.ouroboros/runs'/self.config.run)
        if not source.exists():return
        for file in source.rglob('*'):
            if file.is_symlink() or not file.resolve().is_relative_to(source.resolve()):
                self.recorder.log('publication symlink refused');continue
            if not file.is_file():continue
            relative=file.relative_to(source)
            if str(relative) not in ('story.html','summary.txt',publication.LEDGER) and relative.parts[0]!='assets':continue
            target=view.view_dir(self.control)/relative
            target.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(file,target)

    def run(self):
        # One process owns the entire identity/workspace/checkpoint sequence.
        with (self.control/'runner.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            self.state=json.loads(self.checkpoint.read_text())
            if self.state['fingerprint']!=self.fingerprint:raise ValueError('resume fingerprint changed')
            self.recover()
            while not self._limit():
                try:self.step()
                except (KeyboardInterrupt,SystemExit):
                    self.state['stop_reason']='interrupted_inflight' if self.state['pending'] else 'interrupted'
                    self.save();break
                except Exception as exc:
                    self.state['stop_reason']='runner_error';self.state['runner_error']=repr(exc);self.save();break
            self.state['stop_reason']=self._limit();self.save();self.publish('stopped')
            return self.state['stop_reason']


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,required=True)
    parser.add_argument('--repo',type=Path,required=True)
    parser.add_argument('--control',type=Path,required=True)
    parser.add_argument('--allow-unconfined',action='store_true',help='fixture-only: explicitly permit no filesystem confinement')
    parser.add_argument('--private-a',type=Path);parser.add_argument('--private-b',type=Path)
    args=parser.parse_args(argv)
    try:
        cfg=ExperimentConfig.model_validate(yaml.safe_load(args.config.read_text()))
        if cfg.confinement=='none' and not args.allow_unconfined:
            raise ValueError('unconfined mode requires --allow-unconfined; not a benchmark isolation receipt')
        private={k:p for k,p in [('solver_a',args.private_a),('solver_b',args.private_b)] if p}
        runner=ExperimentRunner(cfg,args.repo,args.control,private_workspaces=private)
        old=signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
        try:print(runner.run())
        finally:signal.signal(signal.SIGTERM,old)
    except (ValueError,OSError) as exc:
        parser.exit(2,f'experiment: {exc}\n')
    return 0


if __name__=='__main__':raise SystemExit(main())
