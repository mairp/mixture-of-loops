# Derivation guide

Use this guide while turning Spec Kit sources into a launch contract. The central risk is
silent omission: a generic test can pass while a command or policy named by the plan was
never exercised.

## Artifact inventory

Start with every authoritative artifact that exists in each supplied feature directory:
`spec.md`, `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `quickstart.md`,
`verification-commands.json`, `contracts/`, checklists, and clarification records. Read
the repository constitution and local instructions. Follow local references only when
they affect execution, and record whether a referenced feature is context or in scope.

Record absent expected artifacts and unsupported formats. Hash raw content, including
dirty and untracked files. A task checkbox is progress state; do not treat a checkbox-only
change as a requirements change when deciding whether a contract is stale. The bootstrap
stores both a raw hash and a checkbox-normalized semantic hash for `tasks.md`; the runtime
still fails stale when any other task content changes.

## Three derivation tiers

1. Deterministic facts: paths, hashes, feature slugs, task IDs, phase headings, existing
   verification JSON, and literal command blocks. Use `bootstrap_contract.py`.
2. Structured prose: map declared commands to task phases, reconcile plan delivery phases,
   and preserve command order, cwd, env, timeout, and source text. Verify each mapping
   against the complete source.
3. Semantic obligations: classify prerequisites, implementation, authorization, automated
   decisions, release criteria, recovery, infrastructure, and observability. Give uncertain
   execution-critical items an open blocker rather than guessing.

Classify each obligation on separate axes:

| Axis | Values |
| --- | --- |
| Kind | `implementation`, `prerequisite`, `verification`, `decision`, `operation` |
| Timing | `pipeline-start`, `before:<stage>`, `after:<stage>`, `release` |
| Producer | `existing`, `stage:<id>`, `automated-policy`, `external-authority`, `unknown` |

An unchecked task is work still to do: its producer is the `stage:<id>` that implements it,
not `existing`, even when files it names are already present. The bootstrap's
`unclassified` producer is a placeholder: strict validation refuses it, and a
`stage:<id>` producer must name a stage.
| Disposition | `mapped`, `optional`, `out-of-scope`, `unresolved`, `unsupported` |

Every entry needs a stable ID, source path and line/anchor, rationale, enforcing stage or
command, and expected evidence. Optional and out-of-scope entries need a reason. Unresolved
or autonomy-incompatible execution requirements need an open blocker.

A prerequisite's path is relative to the repository root unless its source says
otherwise; the bootstrap resolves each one into `inventory.prerequisites` with a `present`
flag, and that flag, not a guess about the base directory, decides whether an authority
exists. An absent non-delegable authority is an open blocker anchored at the prerequisite's
line, and the contract stays a draft; a stage precondition on the same path is not a
substitute, because it would only fail at run time what must block derivation.

## Verification reconciliation

When `verification-commands.json` exists, preserve it as authored input and compare it to
the plan and tasks. Do not select a winner by file age or format. When it is absent, derive
a candidate only from commands actually declared in the source. If commands conflict or a
shell expression cannot be represented as fixed argv, report the exact source and block
that gate.

For each command, reconcile:

```text
source phase/task -> command ID -> Specstride task phase -> phase/release gate
```

Count source declarations, normalized commands, and gate mappings to expose omissions,
collisions, and unintended duplicates. Phases with no declared commands stay explicitly
empty; Specstride's discovered project tests are supplemental and must not be described as the
declared gate.

The installed Specstride interface checked on 2026-09-09 accepts a JSON document containing
`commands[]` with `id`, `phase`, `executable`, `args`, `cwd`, `timeoutSec`, and optional
`env`. It resolves bare executables, rejects missing cwd or unknown phases, and binds the
document hash into its verification plan. references/contract.md ("Verification plan")
gives the exact shape, and strict validation refuses a plan that does not have it. This evidence came from the local Specstride checkout (then still named Wiggum) at revision
`1e3777f3fe16d0bc6d9e0568b4b59e519e4bab9f` with verification-related working-tree
changes, so it establishes local compatibility rather than a released-version guarantee.

## Stage and configuration rules

Each stage declares dependencies, cwd, preconditions, one fixed-argv action, postconditions,
evidence paths, and bounded recovery. Put a prerequisite at the earliest boundary where it
is both needed and expected to exist. Every `present` entry in `inventory.prerequisites`
gets a precondition on its path (strict validation refuses one that has none), unless a
`resolved` or `accepted` finding at its own line records why it gates nothing. Setup stages run only with `--implement`; otherwise
their postconditions must already hold.

Build a Specstride action from these options only; this list is the supported set, so
do not run `specstride --help`, look it up on `PATH`, or read its files to learn more
(SKILL.md: whether it is installed is the launcher's `--dry-run` job to report):
`--spec-format speckit-tasks`, `--feature`, `--proposer`, `--critic`,
`--verification required`, `--verification-commands`, `--test-plan`, `--generate-tests`,
`--telemetry`, `--loki-url`, `--otel`, `--otel-url`, `--live`, `--no-live`, and timeout/
budget controls. `specstride resume` restores saved configuration, but persist explicit
overrides in the resume argv rather than assuming every environment setting was saved.

Declare Specstride's learning mode in each `specstride` stage's `env` when it should be
anything but `off`: `"SPECSTRIDE_LEARNING": "suggest"` or `"apply"`, as a literal. The
runtime strips any inherited value and passes `off` to a stage that declares nothing, so
the mode in the contract is the mode that runs. When the feature has a decision log, keep
the `configuration.learning` block the bootstrap emitted and expand it into every
`specstride` stage's `action.env` and `resume.env` at derivation time:
`SPECSTRIDE_LEARNING` = its `mode` and `SPECSTRIDE_LEARNING_THROUGH` = its
`decisions_through`, both literals. Validation cross-checks the literals against the block;
the runtime injects nothing from it. Choose the mode with `supervise.py learning` (see the
skill's *Learning mode*): `off` by default, `suggest` when the request asks the loop to learn,
`apply` only when it explicitly asks for learned values and the bootstrap bound a decision log
— validation rejects `apply` without one. Never register a file under Specstride's
`learning/` directory as a contract source; validation rejects it (see the contract
reference).

Specstride exit `4` covers wall budget, iteration exhaustion, and consecutive proposer errors.
Only retry it when the newest correlated event records an allowed `run_stop.reason`. Exit
`6` is an intentional stop and must remain stopped. Exit `5` means another run owns the
workdir and is a terminal launcher conflict.

Enable the single Specstride presenter by inheriting the terminal. Do not pipe its output
through a blanket `tee`; Specstride chooses live mode using TTY detection and already stores
raw output in its own `run.log`. In detached execution, use the durable event stream and
attach with `specstride watch`, `specstride events -f`, or `specstride tail` for the selected workdir
and feature.

## Supervision budget

`configuration.auto` is derived, not chosen. Read the bound off the stages that are already
written: one launcher attempt can take at most
`sum(recovery.max_attempts x action.timeout_seconds + sum(backoff_seconds))` over the
stages, and `max_relaunches + 1` attempts can take at most that many times as long. Declare
`wall_clock_seconds` at or below that figure and `max_relaunches` at or below the number of
relaunches the pipeline's own recovery bounds make meaningful; validation rejects anything
larger. Omit the block entirely unless the request or the sources justify departing from
the default of two relaunches, and never widen a budget to make a failing pipeline finish:
an exhausted budget is a result to report, not an obstacle to raise.

A relaunch is not a substitute for a stage's own `recovery`. In-stage retries handle a
transient inside one attempt; the budget handles a launcher that exhausted them and exited
`22`, and the runtime resumes from the first stage whose postconditions no longer hold. If
a stage has no declared transient class, it has nothing for a supervisor to relaunch on
either, and that is the correct outcome rather than a gap to fill.

## Readiness review

Before setting `status` to `validated`, confirm:

- every in-scope execution obligation has a non-unresolved disposition and provenance;
- dependencies are acyclic and all stage/verification references resolve;
- all model, setup, package, auth, and gate subprocesses are noninteractive or fail fast;
- source hashes bind the actual supplied content;
- setup actions are declared, scoped, idempotent, and postcondition checked;
- retries have a failure class, backoff, maximum attempts, and total deadline;
- final acceptance and any release action have executable policy and evidence;
- no success path prints a command that a person must run to finish the pipeline.
