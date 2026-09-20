---
name: mixture-of-loops
description: Derive a complete, unattended Specstride pipeline from one or more Spec Kit feature sets. Use when asked to turn spec.md, plan.md, tasks.md, verification declarations, dependencies, prerequisites, or release gates into a provenance-bound launch contract and executable run script.
---

# Mixture of Loops

Generate an inspectable pipeline from the repository's actual Spec Kit artifacts. The
pipeline must finish, recover within declared bounds, or fail with evidence while stdin
is closed and no terminal is attached.

Resolve relative resources from the directory containing this `SKILL.md`; call that
directory `SKILL_ROOT`. Do not depend on a host-specific variable, hook, subagent, or
tool name.

## Inputs and output

Determine from the request and repository:

- repository root and feature/spec paths;
- output launcher path, defaulting to `.mixture-of-loops/run-<feature>.sh`;
- explicit backend, model, budget, telemetry, infrastructure, and recovery choices.

Keep visible defaults for values the user did not specify. Generation writes a launch
contract and launcher bundle; it does not execute the pipeline unless the user separately
asks to run it.

Every generated artifact belongs under `.mixture-of-loops/` at the repository root: the
contract at `.mixture-of-loops/<feature>/launch-contract.json`, the launcher beside it,
and the bundle and run state in the subdirectories the runtime owns. Leave nothing
generated at the repository root. Honor an output path the user gives instead, including
an existing root-level launcher, which keeps working unchanged.

## Workflow

1. Read [references/derivation.md](references/derivation.md). Inventory all supplied
   feature artifacts and relevant repository instructions before deriving stages.
2. Before writing any artifact, make a Git repository ignore `.mixture-of-loops/`, so the
   first `git status` after generation stays clean. Append the rule once, idempotently,
   and say which file received it. Use the repository's `.gitignore` by default and
   `.git/info/exclude` when the user prefers to leave shared files untouched:

   ```text
   .mixture-of-loops/*
   !.mixture-of-loops/*/
   !.mixture-of-loops/**/launch-contract.json
   ```

   These negations keep the contract committable, since Git does not descend into a
   wholly ignored directory. Offer plain `.mixture-of-loops/` when the user wants the
   contract ignored too.
3. Bootstrap a provenance-bound draft:

   ```text
   python3 SKILL_ROOT/scripts/bootstrap_contract.py \
     --repo REPOSITORY --feature FEATURE_PATH --output LAUNCH_CONTRACT
   ```

   Repeat `--feature` for an explicitly supplied dependent feature set. Do not broaden
   execution scope merely because another feature is referenced for context.
4. Read every inventoried execution-relevant source in full. Complete the contract using
   [references/contract.md](references/contract.md) and
   [assets/launch-contract.schema.json](assets/launch-contract.schema.json). Give every
   obligation provenance and a disposition. Reserve model judgment for semantic
   classification, command-to-phase reconciliation, dependencies, and policy timing.
5. Preserve declared verification commands as fixed `executable` plus `args`; never
   invent a plausible command. An absent or conflicting declaration is an explicit
   finding. Keep Specstride's verification plan separate from the launch contract and pass it
   with `--verification-commands`.
6. Classify prerequisites by producer and earliest valid check. A future stage output is
   not a preflight input. Existing authorization may be consumed within its scope;
   missing non-delegable authority blocks before the affected model run. Never fabricate
   a signature, identity, approval, budget, tolerance, or deployment authority.
7. Make stage order explicit and serial unless actual interfaces and shared-state rules
   prove concurrency safe. Use argv arrays and environment references, never shell
   strings, `eval`, `sh -c`, or blanket answers to prompts.
8. Validate, then render:

   ```text
   python3 SKILL_ROOT/scripts/validate_contract.py LAUNCH_CONTRACT
   python3 SKILL_ROOT/scripts/render_launcher.py \
     --contract LAUNCH_CONTRACT --output RUN_SCRIPT
   ```

   If validation cannot reach `validated`, still leave the reviewable draft and report
   each blocker. Do not render a launch-ready script from unresolved execution-critical
   findings.
9. Run `bash -n RUN_SCRIPT` and invoke `RUN_SCRIPT --dry-run`. Dry-run is read-only and
   takes precedence over `--implement` and `--smoke` in every argument order. Run further
   stubbed checks when the generated setup, decision, or recovery logic warrants them.

## Required runtime behavior

- Default execution uses existing prerequisites. `--implement` may perform only declared,
  idempotent setup actions and must verify their postconditions. `--smoke` adds declared
  smoke stages. `--dry-run` creates no state, logs, directories, or subprocesses.
- Use bounded timeouts and recovery. Retry only classified transient outcomes. A Specstride
  exit code alone is insufficient when it covers several stop reasons; bind recovery to
  the current structured `run_stop.reason`. Preserve intentional stops.
- Revalidate completed-stage postconditions on launcher resume. Refuse changed source
  hashes, a live competing run, dependency cycles, or open blockers.
- Keep terminal labels meaningful without color. Honor `--color=auto|always|never`,
  `--no-color`, and `NO_COLOR`. Let Specstride own its single live presenter; use `--live`,
  `SPECSTRIDE_AGENT_STREAM=true`, and `SPECSTRIDE_LIVE_DETAIL=full` only where supported. Durable
  launcher logs and JSON remain free of ANSI escapes.
- Configure Loki (`--telemetry`/`--loki-url`) and OTLP
  (`--otel`/`--otel-url`) independently. Report observed delivery state without
  converting configuration or reachability into proof of delivery.
- Print a final digest on completion, failure, stop, and interruption with the last stage,
  child result, evidence/state paths, and next automated or operator action.

The generated launcher is a control layer around Specstride, not a second gate authority.
Specstride remains responsible for proposer/critic separation, phase gates, evidence, and its
feature state.
