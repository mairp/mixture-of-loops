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

Every fenced block below is a command line. Run it as one, in a shell, from the
repository root, and read what it prints. Do not re-implement it through another
language's process API: the working directory, the environment and the quoting are part
of the command, and a wrapper is where they get lost. In a notebook-style harness that
means a shell cell (`%%bash`) or a `!` line, not `subprocess.run`.

Work inside the repository and `SKILL_ROOT` only. Do not list or read parent directories,
sibling directories, your home directory or the wider filesystem, and do not search for
`specstride` or other tooling (`which`, `command -v`, `type`, a `PATH` directory's
listing), run it (`specstride --help`), or read its executable: whether it is installed is
the launcher's `--dry-run` job to report (step 9), and the options it takes are the ones
[references/derivation.md](references/derivation.md) lists. `SKILL_ROOT` is the directory as
you reached it; do not follow a symlink out of it to list what surrounds its target. Git
configuration outside the repository (`~/.gitconfig`, `~/.config/git/`,
`core.excludesFile`, `git config --global`) is out of scope too.

## Inputs and output

Determine from the request and repository:

- repository root and feature/spec paths;
- output launcher path, defaulting to `.mixture-of-loops/run-<feature>.sh`;
- explicit backend, model, budget, telemetry, infrastructure, and recovery choices;
- the execution mode.

Keep visible defaults for values the user did not specify.

### Mode

| Mode | Behavior |
| --- | --- |
| `generate` (default) | Steps 1-9 only, through `bash -n` and `--dry-run`. Nothing is executed. |
| `run` | Execute an already generated launcher. No re-derivation, no re-render. |
| `auto` | `generate`, then `run` if and only if the launch gate passes. |

Silence is not consent to execute. A request that does not ask for execution gets
`generate`, and the closing message offers `run` as the next step. Read the mode
deterministically rather than by impression:

```text
python3 SKILL_ROOT/scripts/supervise.py mode --request "THE REQUEST, VERBATIM"
```

It recognizes an explicit `--auto`, `--run` or `--generate` token, which wins over prose,
and otherwise natural phrasing in any harness ("auto", "run it", "generate and run it",
"just derive it", "don't run it"). `--implement` and `--smoke` pass through to the
launcher only when the request asked for them. `--dry-run` is never combined with `run`
or `auto`: it is the generation gate, not an execution mode.

### Learning mode

Specstride's learning loop improves how it drives its agents from its own runs. The harness
does not decide how it improves: it records one choice from the request, the
`SPECSTRIDE_LEARNING` every `specstride` stage runs with. Read it deterministically:

```text
python3 SKILL_ROOT/scripts/supervise.py learning --request "THE REQUEST, VERBATIM"
```

| Mode | When | What the run does |
| --- | --- | --- |
| `off` (default) | the request says nothing about learning, or refuses it ("no learning", "without self-tuning") | nothing is observed or read |
| `suggest` | the request asks the loop to learn or self-improve ("learn from its runs", "make it self-improving", "with learning") | records per-phase observations and evaluates decisions already applied; changes nothing it runs with |
| `apply` | the request explicitly asks to use learned or applied values ("use the learned timeouts", "apply the learned values", `learning: apply`) | runs with the decisions the contract binds, and still evaluates and auto-reverts them |

An explicit `--learning-off`, `--learning-suggest` or `--learning-apply` token wins over prose,
and a refusal wins over any request to learn. `apply` is never inferred from a vague wish.
Pass the mode to `bootstrap_contract.py --learning-mode MODE`. For `apply`, the bootstrap must
emit a `configuration.learning` block, because that block is what binds the decisions the run may
use; if it emits none, the feature has no applied decisions, so use `suggest` and say so.
Validation rejects `apply` without the block. Write the mode, and for a bound block its
`decisions_through` as `SPECSTRIDE_LEARNING_THROUGH`, literally into every `specstride` stage's
`action.env` and `resume.env`. State the learning mode in the closing message beside the
execution mode.

The harness never runs `specstride learn --apply`, `--revert` or `--off`. Applying a decision is
the operator's act; Specstride measures, evaluates and reverts on its own inside the run; and
`supervise.py retro` may only suggest a revert.

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
   `.git/info/exclude` when the user prefers to leave shared files untouched. Read and
   write only those two files: never the global excludes file or any git config outside
   the repository, even to check whether a rule already exists there:

   ```text
   .mixture-of-loops/*
   !.mixture-of-loops/*/
   !.mixture-of-loops/**/launch-contract.json
   ```

   These negations keep the contract committable, since Git does not descend into a
   wholly ignored directory. Offer plain `.mixture-of-loops/` when the user wants the
   contract ignored too.
3. Bootstrap a provenance-bound draft, with the learning mode read above. This is a shell
   command line, not a call to make through a language's process API (see above):

   ```text
   MOL_VIA=shell python3 SKILL_ROOT/scripts/bootstrap_contract.py \
     --repo REPOSITORY --feature FEATURE_PATH --learning-mode MODE --output LAUNCH_CONTRACT
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

   Paths a prerequisite names are relative to the repository root, not to the feature
   directory. The draft's `inventory.prerequisites` lists each one as written, where it
   resolves under `repository.root`, and whether it is `present`; read existence from
   there rather than guessing a base. A non-delegable authority that is not `present` is
   an open `blocker` finding whose `source` is the prerequisite's own line, and it stops
   derivation at a draft: a runtime precondition check on the same path does not resolve
   it, because the authority must exist before the pipeline is derived, not before it
   runs. A `present` one is consumed within its scope: give it a precondition on its path
   at the earliest stage that needs it. Strict validation refuses a present prerequisite
   no stage checks.

   `file_exists` is for a file. For a directory, Specstride's feature state directory
   `.specstride/features/<slug>` included, use `dir_exists`; validation refuses
   `file_exists` on either.
7. Make stage order explicit and serial unless actual interfaces and shared-state rules
   prove concurrency safe. Use argv arrays and environment references, never shell
   strings, `eval`, `sh -c`, or blanket answers to prompts.

   An implementation obligation (every task the bootstrap inventoried starts as one) maps
   to a `specstride` stage: implementing it is Specstride's loop, not yours. Never write
   the implementation yourself, neither into the repository nor into a stage's argv
   (`python3 -c "open('src/x.py','w').write(...)"`): `setup` stages are for declared,
   idempotent environment setup and `command` stages for declared verification. Strict
   validation refuses a mapped implementation obligation with no `specstride` stage.
8. Validate, then render. Same rule as step 3 — a shell command line, not a subprocess call:

   ```text
   MOL_VIA=shell python3 SKILL_ROOT/scripts/validate_contract.py --promote LAUNCH_CONTRACT
   MOL_VIA=shell python3 SKILL_ROOT/scripts/render_launcher.py \
     --contract LAUNCH_CONTRACT --output RUN_SCRIPT
   ```

   Never write `status` yourself, and never write a contract from scratch: derive from the
   bootstrap's draft. The bootstrap writes `draft`, and only `--promote` writes `validated`,
   and only when the strict check passes. It stamps what it validated, and the renderer
   refuses `validated` without that stamp or after any later edit: promote after every
   change, and render only after it exits 0. While any `blocker` finding
   is `open`, `--promote` exits 20, leaves (or sets) `draft`, and prints each blocker. A
   `next:` line names work the draft still owes (derivation not done, or an absent
   prerequisite with no blocker at its line): do it, then promote again. Without `next:`
   lines, that draft, with its blockers, is the deliverable for a blocked pipeline. Do not
   render a launch-ready script from it, and do not resolve a blocker to make the check pass.
9. Run `bash -n RUN_SCRIPT` and invoke `RUN_SCRIPT --dry-run`, the same shell command line
   style as steps 3 and 8 (neither call is one of the skill's scripts, so there is no
   marker to carry):

   ```text
   bash -n RUN_SCRIPT
   RUN_SCRIPT --dry-run
   ```

   Dry-run is read-only and takes precedence over `--implement` and `--smoke` in every
   argument order. Run further stubbed checks when the generated setup, decision, or
   recovery logic warrants them. In `generate` this is where the work ends; say so, and
   offer `run`. In `auto` the generated artifacts are complete at this point: go straight
   to step 11 with the path you just rendered. Do not revisit steps 1-8, and do not
   re-check the ignore rule, the contract or the launcher again — the gate in step 11
   checks all of it, by name.

## Execution

Only in `run` and `auto`. Every step below is one command and one file to read, so any
harness can perform it; nothing here depends on a host-specific hook, subagent, scheduler
or tool name.

10. In `run` only, find the launcher; in `auto` you already have its path. Never
    generate one silently.

    ```text
    python3 SKILL_ROOT/scripts/supervise.py resolve \
      --repo REPOSITORY --feature FEATURE_PATH [--launcher RUN_SCRIPT]
    ```

    It takes a launcher named in the request, else
    `.mixture-of-loops/<feature>/run-<feature>.sh`, else the operator-chosen output path,
    recovered from the generated marker the launcher carries. If none exists, report what
    is missing and offer `auto`. `run` re-derives nothing: if the sources moved under the
    launcher, the runtime's own stale check (exit `23`) is the authority; report it and
    offer `auto`.

11. Know the launch gate. `launch` and `auto` below run it themselves and print every
    check, so run it separately only to report the checks without launching:

    ```text
    python3 SKILL_ROOT/scripts/supervise.py gate --launcher RUN_SCRIPT
    ```

    A launch needs all six: the launcher's `generated-content-sha256` is intact; the
    bundled contract is `validated` with current sources and no open blocker; the
    repository root is inside `authorized_roots`; `bash -n` passes; `--dry-run` exits `0`;
    and no live run owns the run directory. A held lock means "already running — attach
    and report", never a failure to retry. Whenever the gate stops a launch, deliver the
    generated artifacts anyway and state the one blocking reason plus the smallest action
    that would clear it. Never edit a launcher and never pass `--replace-edited` on the
    user's behalf.

12. Launch and supervise:

    ```text
    python3 SKILL_ROOT/scripts/supervise.py auto \
      --launcher RUN_SCRIPT [--implement] [--smoke]
    ```

    This gates, starts the launcher detached, and reports until the run is terminal. A
    harness with a scheduling primitive may instead run `launch` once and `observe` at each
    wake-up; the messages are identical either way, and `observe` prints the delay to wait
    before the next one. To end a run the user asked to end, use `stop --launcher
    RUN_SCRIPT`, or the `kill -TERM -<pgid>` the launch message prints.

13. Relay the `[MOL-*]` lines as they are printed, and add nothing to them. Report the
    terminal digest, and stop.

    Every one of those lines is also appended to `runs/<id>/harness-report.log`. If the
    harness moves a long-running supervision command to the background, truncates its
    output, or loses the stream some other way, read that file and relay the lines from
    there rather than describing the run in your own words.

### Launch discipline

The launcher runs detached so the pipeline outlives the harness turn: stdin closed, a
session of its own, color forced off, and both streams redirected to
`runs/<id>/harness-launch.log`, which is separate from the runtime-owned `launcher.log`.
The command is an argv array; never a `sh -c` string, an `eval`, or a shell-interpolated
path. Immediately afterwards a harness-owned launch record is written to
`runs/<id>/harness-run.json`, holding the launcher, the exact argv, the child PID and
process group, the launch time, the contract digest observed at launch, the mode, and the
relaunch budget with its remaining count. It is the supervisor's own memory and never
substitutes for `state.json` when reporting. Nothing generated is written outside
`.mixture-of-loops/`.

### What the supervisor reads

In this order of authority: `runs/<id>/state.json`; then liveness of the recorded PID,
which separates "running" from "died without writing a terminal state"; then
`runs/<id>/launcher.log` for the labelled stream and the final `DIGEST`; then Specstride's
`run_stop.reason` JSONL under the stage's workdir state dir, the same source the
launcher's own retry classification binds to; and last `runs/<id>/harness-launch.log`, only
for a launch that died before the runtime could report. Every file may be absent or behind;
a missing `state.json` before preflight is normal, and an unparsable one is a reported
anomaly rather than a crash. The supervisor only reads: it never edits `state.json`, never
removes a lock, and never deletes a run directory for a cleaner start. Progress comes from
telemetry alone — never infer a stage, a percentage, or an ETA from the child's prose or
from your own narration. Loki and OTLP may be reported as configured or observed, never as
proof that telemetry was delivered.

### What the user is told

- **On launch**: one line naming the launcher, the run directory, the pipeline id, the
  stage count, the mode and flags in effect, the relaunch budget, and how to stop the run.
- **While running**: state lines carrying the stage position `k/n` in contract order, the
  stage id, its elapsed time, the run's total elapsed time, and the latest recovery or
  block detail when there is one. Nothing else.
- **Cadence**: no tighter than one poll per minute, backing off as a stage runs long and
  returning to the base interval at a stage boundary or after a `RECOVER`. Consecutive
  unchanged observations are one quiet hold, not the state repeated.
- **On terminal state**: one digest mirroring the launcher's own `DIGEST` line — state,
  launcher exit, child exit, last stage, evidence and state paths — plus the next
  automated or operator action. `completed`, `failed`, `stopped` and unknown-terminal each
  read differently; never flatten them into "done", and never claim completion without both
  a terminal `state.json` and an exited process.

### Relaunching

Re-invoke the launcher only when the run's own records classify the stop as transient: the
launcher exited `22`, `state.json` reads `failed`, the failed stage's `child_exit_code` is
in that stage's `recovery.retry_exit_codes`, the newest `run_stop.reason` is in
`recovery.reason.allowed` where the stage declares one, the stage's recorded reason is a
transient outcome and not `postcondition`, `stage-precondition`, `setup-required` or
`interrupted`, the contract digest still matches the one recorded at launch, and the budget
has room. Resume is re-invocation: the runtime reconciles against its own `state.json`.

Never relaunch on an invalid or stale contract, a preflight block, a lock conflict, a
deliberate stop (a signal, or child exit `6`), a postcondition failure, a missing authority
or open blocker, an unknown-terminal condition, or an exhausted budget. Report and stop.
Every relaunch is announced before it happens, with its reason and the remaining budget,
and each is recorded in `harness-run.json`. A stop the user asked for is reported as
intentional and is never relaunched. The budget is declared, not improvised: it comes from
`configuration.auto`, or from the conservative default derived from the stages' own
declared timeouts and attempts.

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

The generated launcher is a control layer around Specstride, not a second gate authority,
and supervising it is not one either. Specstride remains responsible for proposer/critic
separation, phase gates, evidence, and its feature state. The supervisor decides only when
to launch, what the run's own telemetry says, and whether a declared, classified transient
may be relaunched within a declared budget. It never decides that a gate passed, and it
never fabricates a signature, identity, approval, budget, tolerance, or deployment
authority to keep a run going.
