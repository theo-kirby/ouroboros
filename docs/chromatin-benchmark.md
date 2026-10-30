# Chromatin CPU research-system benchmark — candidate design

2026-10-29. Theo selected the earlier Lab chromatin project as the next benchmark direction, replacing CPU candidate brainstorming. No new run is launched by this document.

## Scientific question

Does chromatin accessibility add reproducible information about regional somatic mutation rates once sequence opportunities, observation support and replication timing are accounted for—and what can the available data actually establish?

## Evidence and scope

Historical source: /home/theo/research-projects/chromatin-mutations.
Native successor: /home/theo/research-projects/chromatin-native.
Read NATIVE-HANDOFF.md, CHARTER.md, reports/native-progress.md and the October24 nightly chromatin assessment before selecting work. Preserve both checkouts, historical graphs and untracked alignment artifacts.

The old liver pilot reports eight donors, 44,400 mutations and 1,556 windows, an adjusted rate ratio around0.904, and predictive gains on11/16 held-out chromosomes. These are historical descriptive outputs, not verified biological ground truth. The subsequent audit found donor-specific mutation/opportunity domain mismatch; sequence opportunities were not consistently masked. Eligibility is not validated detection sensitivity. Timing is a HepG2 proxy, highly correlated with the accessibility measure. Present-day accessibility cannot establish historical necessity or causal protection.

The later SEQC2 read/template/alignment diagnostics are completed prior work. They are not an unanswered chromatin experiment and cannot calibrate PCAWG detection sensitivity by substitution. October24 explicitly excluded a redundant17-locus continuation.

## Proposed controlled task

Create a separate experiment repository and frozen input packet, with its own local memory graph. Ask agents to answer the scientific question using retained public inputs, executable counts, competing models, appropriate blocked validation and a reader-facing report. Permit a justified non-identifiability conclusion. No requirement to produce a positive association or novel biological discovery.

Separate two purposes: a retrospective known-answer audit benchmark tests detection/repair of documented measurement errors; a prospective scientific extension requires a genuinely unanswered, feasible contrast. Do not describe the former as new science. Do not mix the two in a pooled score.

Keep earlier final analyses and evaluation answers outside the agent packet where practical; provide necessary source provenance and input definitions. Record that historical task knowledge can still be available through public retrieval/model memory. Evaluator isolation must be enforced outside the agent workspace, not by hidden filenames in a readable repository.

## Preparation and admission

1. Inventory retained inputs and rights, pin source revisions/file hashes, and determine a complete bounded domain with donor/mask/reference/context identities.
2. Independently reproduce a matched recount and quantify what it changes. Freeze numerical evaluator tolerances and scientific claim limits; old fitted coefficients are not acceptance targets.
3. Measure baseline CPU time and RAM for counting, fitting and blocked validation. Desired total agent duration1–2hours is unmeasured; avoid manufacturing difficulty through redundant computation.
4. If a valid predictive comparison is supportable, define its holdouts and metric before comparisons. If not, use an explicit audit/recovery task with a finite, verifiable conclusion rather than letting agents search indefinitely for inaccessible data.
5. Freeze the task, compute/thread/time budgets, confirmation checks and communication rubric before system comparisons. Run one bounded pilot before a batch.

## Evaluation

Score numerical correctness and domain matching separately from biological interpretation. Assess evidence for incremental prediction, confounding, observation bias, leakage and independent checks. Measure all attempts, CPU seconds, wall time, all-role usage, decisions, recovery and stopping. More tasks or longer reports do not imply better science.

The native HTML report is the scientific communication artifact. Require a plain-language answer, effect scale if defensible, one decisive explanatory visualization, a concrete counterexample/limitation and linked methods/data. Human comprehension judgments remain separate from automated completeness checks.

Next action: assemble and time the retained-input audit packet, then decide whether this is an audit benchmark or an admitted new scientific contrast. No paid compute or legacy-controller restart is needed for preparation.

## Preparation and ten-run proposal — October29

Dedicated project: /home/theo/research-projects/chromatin-evaluation. Retained inputs
are copied and hash-checked; historical source checkouts/graphs are untouched.
packet/ contains task/provenance/output schema; operator/ holds private reference
answers and must be excluded by actual launch isolation.

Five proposed configurations, two fresh repetitions each: current control;
experimental actor charter with explicit stopping guidance; assumption-focused
critic; dependency-aware verification reuse; explanatory publication template.
Same model/effort/backend/resources throughout. Exact design and limitations:
operator/ten-run-design.md. This chromatin-specific proposal adds an actor-charter
experiment by combining hypothesis-selection and stopping guidance in one arm;
it does not independently identify their contributions. No new campaign launched.

Reference arithmetic calibration and independent oracle receipts/results live in
operator/evidence and operator/results.md. Reference timing is not agent completion
time. The dataset is a retrospective audit benchmark, not a new causal study.

## Screen expanded by Theo — October29

Ten configurations, one execution each, replacing five pairs. Retain control,
experimental charter/stopping, critic focus, verification reuse and publication
structure. Add before-every-actor planner, one persistent freeflow agent and two
role-free alternating peers. Additional proposed arms: blind second solution and
combined charter/critic/publication guidance. Same total all-agent budget; topology
changes include context/memory ownership and must be tested explicitly. Current
engine exposes actor/critic/scribe only; no new topology is implemented by this note.
Repeat promising configurations with fresh controls before promotion.

## Runner implementation — October29

The separate experiment adapter now implements standard/planner/freeflow/peers/blind
execution; see docs/experimental-runner.md. Ten draft configuration files are in
the experiment project operator/configs/. Credential-free subprocess smoke runs
exercise all five shapes and the real memory CLI/native dashboard. Authentication
is deliberately not exposed to agents; a live model smoke test and the scientific
pilot remain pending a safe credential boundary. No comparison campaign launched.
