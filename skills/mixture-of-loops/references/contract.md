# Launch contract and runtime

The launch contract is pipeline data. It does not replace Specstride's verification plan or
`.specstride` state. Schema version `1.0` is defined in
`assets/launch-contract.schema.json` and enforced more strictly by
`scripts/validate_contract.py`.

## Required top-level fields

- `schema_version`: `"1.0"`.
- `id`: stable kebab-case pipeline identifier.
- `status`: `draft` or `validated`.
- `repository.root`: absolute repository root used to resolve relative paths.
- `authorized_roots`: absolute roots under which stage working directories may resolve.
- `sources`: relative or absolute source paths with SHA-256 digests and kinds. A path
  under a `learning/` directory of a `.specstride/` or legacy state tree is rejected:
  Specstride rewrites `learning/phase-<N>.json` at every approved phase, so hashing it would
  refuse every relaunch with exit 23.
- `coverage`: execution obligations with source, classification, disposition, mappings,
  rationale, and evidence expectation.
- `findings`: findings with `severity`, `status`, provenance, and resolution.
- `stages`: dependency-ordered stage records.

The renderer accepts only `validated` contracts with current source hashes and no open
blockers. `bootstrap_contract.py` intentionally creates a draft with unresolved coverage.

## Stages

Stage `kind` is one of `setup`, `decision`, `command`, `specstride`, or `smoke`.

Specstride was formerly Wiggum. Contracts written before the rename still validate:
the stage kind `wiggum` is a deprecated alias that validation normalizes in memory to
`specstride` and reports as a warning, never an error. The same normalization points a
legacy `wiggum` command (argv or `command_available` check) at `specstride`, renames
`WIGGUM_*` action env keys to `SPECSTRIDE_*`, reads `configuration.wiggum_live` as
`specstride_live`, and reads coverage timing `wiggum-phase:N` as `specstride-phase:N`.
Paths under a workdir's `.specstride/` or legacy `.wiggum/` state dir resolve the way
Specstride resolves them: `.specstride/`, unless only the legacy dir exists. For a
`specstride` stage in `never` color mode the runtime sets both `SPECSTRIDE_LIVE=false`
and `WIGGUM_LIVE=false`, so older checkouts behave the same.

```json
{
  "id": "run-feature",
  "kind": "specstride",
  "depends_on": ["prepare-verification"],
  "cwd": ".",
  "action": {
    "argv": ["specstride", "run", "-w", ".", "-s", "specs/007/tasks.md",
      "--spec-format", "speckit-tasks", "--feature", "007", "--live"],
    "env": {
      "SPECSTRIDE_AGENT_STREAM": "true",
      "SPECSTRIDE_LIVE_DETAIL": "full",
      "TOKEN": {"from_env": "FEATURE_TOKEN", "required": true}
    },
    "timeout_seconds": 14400
  },
  "resume": {
    "argv": ["specstride", "resume", "-w", ".", "--feature", "007"],
    "timeout_seconds": 14400
  },
  "preconditions": [
    {"type": "command_available", "name": "specstride", "timing": "preflight"}
  ],
  "postconditions": [
    {"type": "file_exists", "path": ".specstride/features/007/PROGRESS.md"}
  ],
  "recovery": {
    "max_attempts": 2,
    "backoff_seconds": [5],
    "retry_exit_codes": [4],
    "reason": {
      "jsonl": ".specstride/features/007/events.jsonl",
      "event": "run_stop",
      "field": "reason",
      "allowed": ["wall_budget"]
    }
  },
  "evidence": [".specstride/features/007"]
}
```

`action.argv` and `resume.argv` are arrays executed without a shell. Environment values are
literal strings or references to an existing variable. Secret references are resolved only
for execution and are redacted from output and state.

Specstride's learning mode is declared, never inherited. The runtime removes
`SPECSTRIDE_LEARNING`, and its legacy spelling, from the environment every stage action and
every `command_success` check inherits, and passes `SPECSTRIDE_LEARNING=off` explicitly to
each `specstride` stage whose `env` does not declare it. A stage that wants another mode
writes it as a literal: `"SPECSTRIDE_LEARNING": "suggest"` or `"apply"`. Validation rejects
any other value, rejects `apply` without a `configuration.learning` block binding the decisions
it may use, and rejects a `from_env` reference for this variable, because a reference
would pass the operator's shell value through and re-open the gap the strip closes: a
learning mode that the contract, and so its digest, does not record. The value is passed
explicitly rather than left unset because Specstride re-asserts an exported variable over a
`.env` in its checkout, and does not protect an absent one. An `env_set` check still reads
the harness's own environment, so a contract can observe an inherited value but never hand
it to a child.

Supported checks are:

- `file_exists` and `dir_exists` with `path`;
- `env_set` with `name`;
- `command_available` with `name`;
- `command_success` with fixed `argv`, optional `cwd`, `env`, and `timeout_seconds`;
- `json_field_equals` with `path`, dotted `field`, and JSON `value`.

`timing: "preflight"` evaluates a check before any stage action. Other checks run at the
owning stage boundary. A setup stage without `--implement` runs no action and must already
satisfy its postconditions. Smoke stages run only with `--smoke`.

Recovery defaults to one attempt. More attempts require explicit retry exit codes. For
ambiguous Specstride outcomes, include a correlated JSONL reason constraint. A retry uses
`resume` after the first action when supplied.

## Supervision budget

`configuration.auto` is optional. It declares how a supervising harness may treat a run of
this pipeline, and nothing else in the contract depends on it. A contract without it keeps
validating and running exactly as before.

```json
{
  "configuration": {
    "auto": {
      "max_relaunches": 2,
      "wall_clock_seconds": 3600
    }
  }
}
```

- `max_relaunches`: how many times the harness may re-invoke the launcher after a
  classified transient stage failure. An integer from `0` to `5`; `0` forbids relaunching.
- `wall_clock_seconds`: the ceiling on the whole supervised session, measured from the
  first launch. It may not exceed what the stages themselves declare for
  `max_relaunches + 1` launcher attempts, which is the sum over stages of
  `recovery.max_attempts x action.timeout_seconds` plus every declared backoff.
  `validate_contract.py` rejects a larger value and names the bound it exceeded.

Absent, the conservative default applies: at most two relaunches, and a ceiling of the wall
clock those three attempts declare. The budget is derived from the contract, never chosen
by the model at run time.

## Learned decisions

`configuration.learning` is optional. It binds the part of Specstride's decision log that
existed at derivation time into the contract, so the digest records which learned values a
run may act on:

```json
{
  "configuration": {
    "learning": {
      "mode": "apply",
      "decisions_through": "learn-3f2a9c1b0d4e",
      "decisions_sha256": "<SHA-256 of applied.json up to and including that run id's line>",
      "effective": {"proposer_timeout": {"3": 2700}},
      "source_path": ".specstride/features/007/learning/applied.json"
    }
  }
}
```

- `mode` is the `SPECSTRIDE_LEARNING` every `specstride` stage declares.
- `decisions_through` is the `run_id` of the last `applied.json` line bound. Every
  `specstride` stage declares it as `SPECSTRIDE_LEARNING_THROUGH`, a literal, in both
  `action.env` and `resume.env`; Specstride's `resolve` then ignores any `apply` written
  after it. A stage that declares `SPECSTRIDE_LEARNING_THROUGH` with no block is rejected.
- `decisions_sha256` hashes that prefix, and `source_path` locates it relative to
  `repository.root`. Only the prefix is bound, so later appends never make the contract
  stale. When the prefix itself changes, validation with source checks fails with
  `learning-decisions-changed`: the launch gate refuses by that name, and so does the relaunch
  classifier, which the contract digest alone cannot tell (the log is not a source; hashing
  it whole would refuse every relaunch). `applied.json` is never registered in `sources[]`.
- `effective` is the value in effect per knob and phase at `decisions_through`. It is an
  **upper bound** on what runs, not a promise: a revert after derivation — Specstride's
  automatic revert on a guardrail breach, `--revert` or `--off` — still takes effect, because
  it only moves a knob toward its default. The `arm` Specstride records on each pass says
  what actually ran.

`bootstrap_contract.py` fills the block from `--applied-file` (or, for one feature, that
feature's `learning/applied.json`), bound through its last line, with the mode from
`--learning-mode` (default `off`).

## Generated bundle

`render_launcher.py` publishes a content-addressed bundle below
`.mixture-of-loops/generated/<id>/<contract-hash>/`, beside the launcher, and atomically
replaces the stable launcher last. A launcher rendered into `.mixture-of-loops/` itself
uses that directory rather than nesting a second one below it, so bundles and run state
hold the same paths as they do for a launcher at the repository root. The bundle contains the exact contract and runtime. An interrupted render
therefore leaves the previous launcher pointing at a complete previous bundle.

The renderer refuses to replace a launcher whose generated digest no longer matches. Put
maintained changes in the contract or choose a different output path; use
`--replace-edited` only as an explicit regeneration decision.

At execution time the runtime stores plain logs and JSON state below
`.mixture-of-loops/runs/<id>/`. It revalidates the contract sources, obtains a nonblocking
pipeline lock, validates completed-stage postconditions, and resumes from the first stage
whose postconditions no longer hold. State is bound to a semantic contract digest; task
checkbox progress is normalized, while a changed requirement, stage, policy, or source
binding starts a fresh stage record. A supervisor that relaunches across such a change
would restart the pipeline without saying so, which is why it compares the digest in
`state.json` with the one it recorded at launch and stops instead.

A supervising harness adds files of its own to the same directory and reads everything
else there without writing to it:

- `harness-run.json`: the launcher path, the exact argv, the child PID and process-group
  id, the launch time, the contract digest observed at launch, the mode and flags, the
  byte offset into `launcher.log` at which this run's lines begin, and the relaunch budget
  with its remaining count and each relaunch's justification. It is the supervisor's own
  memory and never substitutes for `state.json` when reporting.
- `harness-launch.log`: the detached launcher's stdout and stderr, separate from the
  runtime-owned `launcher.log`. It is where a launch that died before the runtime could
  report leaves its evidence, and where the `[STALE]` and `[INVALID]` paths print, since
  those precede the launcher log.
- `harness-report.log`: every `[MOL-*]` line the supervisor printed, appended as it was
  printed and free of escape sequences. A harness that backgrounds a long-running
  supervision command, or truncates its output, can recover the same messages from here
  instead of from whatever file it happened to redirect to.
- `retrospectives/<contract-digest>.json`: written by `supervise.py retro`, one file per
  contract digest, because `runs/<id>` is keyed on the pipeline and reused across every
  relaunch and re-derivation. For each `specstride` stage it records what Specstride's
  learning layer reports through `specstride learn --summarize` and `--evaluate` (both
  read-only), and the `specstride learn --revert <run-id>` it would suggest for a decision
  evaluated `regressed`. It never runs `apply` or `revert` and writes nothing outside the
  run directory. The stage's Specstride feature is found from `recovery.reason.jsonl`, then
  `evidence[0]`, then a postcondition path naming `.specstride/features/<slug>` (or the
  legacy state dir); a stage with none of them, or a Specstride without those flags, is
  reported `unavailable` and the command still exits 0.

Color precedence is `--no-color`, an explicit `--color`, a present `NO_COLOR`, then
automatic TTY detection. In `always` mode with redirected output, the runtime gives a Specstride stage a
pseudo-terminal so Specstride's own `--live` presenter remains the sole colored presenter. In
`never` mode it replaces `--live` with Specstride's supported `--no-live`, supplies a non-TTY
pipe, forwards the plain stream, and strips ANSI from the launcher log. Other modes inherit
the terminal directly; Specstride continues to own its raw `run.log` and event stream.

Launcher exit codes are `0` success/dry-run, `20` invalid contract, `21` preflight blocker,
`22` stage failure or exhausted recovery, `23` stale source, and `24` pipeline lock held.
Signals return conventional `128 + signal`; a child exit code is recorded in state and the
final digest rather than being confused with the launcher's code.
