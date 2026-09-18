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
| Disposition | `mapped`, `optional`, `out-of-scope`, `unresolved`, `unsupported` |

Every entry needs a stable ID, source path and line/anchor, rationale, enforcing stage or
command, and expected evidence. Optional and out-of-scope entries need a reason. Unresolved
or autonomy-incompatible execution requirements need an open blocker.

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
document hash into its verification plan. Recheck `specstride run --help` when targeting a
different installation. This evidence came from the local Specstride checkout (then still named Wiggum) at revision
`1e3777f3fe16d0bc6d9e0568b4b59e519e4bab9f` with verification-related working-tree
changes, so it establishes local compatibility rather than a released-version guarantee.

## Stage and configuration rules

Each stage declares dependencies, cwd, preconditions, one fixed-argv action, postconditions,
evidence paths, and bounded recovery. Put a prerequisite at the earliest boundary where it
is both needed and expected to exist. Setup stages run only with `--implement`; otherwise
their postconditions must already hold.

Build a Specstride action using only options supported by the target installation. Current
local support includes `--spec-format speckit-tasks`, `--feature`, `--proposer`, `--critic`,
`--verification required`, `--verification-commands`, `--test-plan`, `--generate-tests`,
`--telemetry`, `--loki-url`, `--otel`, `--otel-url`, `--live`, `--no-live`, and timeout/
budget controls. `specstride resume` restores saved configuration, but persist explicit
overrides in the resume argv rather than assuming every environment setting was saved.

Specstride exit `4` covers wall budget, iteration exhaustion, and consecutive proposer errors.
Only retry it when the newest correlated event records an allowed `run_stop.reason`. Exit
`6` is an intentional stop and must remain stopped. Exit `5` means another run owns the
workdir and is a terminal launcher conflict.

Enable the single Specstride presenter by inheriting the terminal. Do not pipe its output
through a blanket `tee`; Specstride chooses live mode using TTY detection and already stores
raw output in its own `run.log`. In detached execution, use the durable event stream and
attach with `specstride watch`, `specstride events -f`, or `specstride tail` for the selected workdir
and feature.

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
