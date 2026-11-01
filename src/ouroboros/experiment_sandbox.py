"""Linux Landlock confinement for evaluation harnesses and all tool descendants.

No mount/user namespace needed. Default-deny filesystem access; allowlisted system
runtime files, one workspace and one identity home. This is not network isolation.
"""
from __future__ import annotations
import argparse
import ctypes
import os
import resource
from pathlib import Path
import sys

CREATE, ADD, RESTRICT = 444, 445, 446
libc=ctypes.CDLL(None,use_errno=True)


def syscall(number,*args):
    result=libc.syscall(number,*args)
    if result<0:
        code=ctypes.get_errno();raise OSError(code,os.strerror(code))
    return result


def abi():
    if sys.platform!='linux':raise OSError('Landlock requires Linux')
    return syscall(CREATE,0,0,1)


class Ruleset(ctypes.Structure):
    _fields_=[('handled_access_fs',ctypes.c_uint64)]


class PathRule(ctypes.Structure):
    _pack_=1
    _fields_=[('allowed_access',ctypes.c_uint64),('parent_fd',ctypes.c_int32)]


def confine(read_paths,write_paths):
    version=abi()
    if version<3:raise RuntimeError('Landlock ABI3+ required for truncate restrictions')
    handled=(1<<15)-1
    ruleset=Ruleset(handled)
    fd=syscall(CREATE,ctypes.byref(ruleset),ctypes.sizeof(ruleset),0)
    try:
        for path,writable in [(p,False) for p in read_paths]+[(p,True) for p in write_paths]:
            path=Path(path)
            access=handled if writable else (1<<0)|(1<<2)|(1<<3)
            if not path.is_dir():
                access &= (1<<0)|(1<<1)|(1<<2)|(1<<14)
            parent=os.open(path,os.O_PATH|os.O_CLOEXEC)
            try:
                rule=PathRule(access,parent)
                syscall(ADD,fd,1,ctypes.byref(rule),0)
            finally:os.close(parent)
        if libc.prctl(38,1,0,0,0)!=0:raise OSError(ctypes.get_errno(),'PR_SET_NO_NEW_PRIVS failed')
        syscall(RESTRICT,fd,0)
    finally:os.close(fd)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read',action='append',default=[])
    parser.add_argument('--write',action='append',default=[])
    parser.add_argument('--cpu',type=int)
    parser.add_argument('--memory-bytes',type=int)
    parser.add_argument('command',nargs=argparse.REMAINDER)
    args=parser.parse_args(argv)
    command=args.command[1:] if args.command[:1]==['--'] else args.command
    if not command:parser.error('command required')
    try:
        # The helper's /proc/self is its own process, not all of /proc. Children
        # cannot use other processes' fd/environ paths to inspect private state.
        paths=args.read+[f'/proc/{os.getpid()}']
        if args.cpu is not None:os.sched_setaffinity(0,{args.cpu})
        if args.memory_bytes is not None:
            resource.setrlimit(resource.RLIMIT_AS,(args.memory_bytes,args.memory_bytes))
        confine(paths,args.write)
        os.execvpe(command[0],command,os.environ)
    except (OSError,RuntimeError) as exc:
        print(f'experiment confinement failed: {exc}',file=sys.stderr);return 126


if __name__=='__main__':raise SystemExit(main())
