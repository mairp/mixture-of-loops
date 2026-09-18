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
- `sources`: relative or absolute source paths with SHA-256 digests and kinds.
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

## Generated bundle

`render_launcher.py` publishes a content-addressed bundle below
`.mixture-of-loops/generated/<id>/<contract-hash>/` and atomically replaces the stable
launcher last. The bundle contains the exact contract and runtime. An interrupted render
therefore leaves the previous launcher pointing at a complete previous bundle.

The renderer refuses to replace a launcher whose generated digest no longer matches. Put
maintained changes in the contract or choose a different output path; use
`--replace-edited` only as an explicit regeneration decision.

At execution time the runtime stores plain logs and JSON state below
`.mixture-of-loops/runs/<id>/`. It revalidates the contract sources, obtains a nonblocking
pipeline lock, validates completed-stage postconditions, and resumes from the first stage
whose postconditions no longer hold. State is bound to a semantic contract digest; task
checkbox progress is normalized, while a changed requirement, stage, policy, or source
binding starts a fresh stage record.

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
