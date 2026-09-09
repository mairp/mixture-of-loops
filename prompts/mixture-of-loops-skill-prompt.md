# Agent prompt: design the autonomous mixture-of-loops skill

Enhance `/root/wiggum/roadmap/speckit-pipeline-skill.md` into an implementation-ready design for a reusable skill named **mixture-of-loops**.

The intended user experience is:

> Read this repository's Spec Kit specifications holistically and write a complete, executable pipeline script that runs end to end autonomously, without human intervention, with colored operational messages and full visibility into Wiggum's execution and telemetry.

The same maintained skill must work in **Codex and Claude Code**. Its purpose is to derive a pipeline from the supplied specifications, including their dependencies, implementation tasks, verification gates, environment requirements, automated decision policies, and operational lifecycle. `/root/agentflow/run-005-all.sh` is a historical reference to improve, not a template for retaining manual steps.

## Non-negotiable outcome: fully autonomous execution

Once invoked with its configuration, the generated pipeline must complete all in-scope stages without interactive questions, approval dialogs, human checkpoints, manual artifact editing, or manual resume commands. Splitting the work into runs separated by a manual handoff does not satisfy this requirement. It must work with stdin closed and no attached terminal. Colored live viewing is an optional observation channel, never a control dependency.

Derive routine choices, produce machine-authorable inputs, evaluate automated gates, perform authorized setup, recover from classified transient failures, and resume within explicit retry/time/budget limits. Preserve the proposer/critic separation and evidence requirements. Completion and any in-scope release action must be determined by executable acceptance criteria and configured policy, not by asking an operator to finish the run. Do not add approval ceremonies merely because a task involves infrastructure, model selection, or generated files.

Audit inherited manual steps individually. Replace them with automated producers, checks, or decisions wherever the user's authorization and applicable requirements permit. If a source explicitly requires an external human signature or decision that cannot be delegated, identify that incompatibility before launch; never silently forge it, bypass it, or implement a runtime wait for a human. Existing valid authorization may be consumed automatically. A feature requiring a new, non-delegable human decision during execution is outside the autonomous MVP until that requirement changes; do not call such a pipeline autonomous or launch-ready.

Autonomous execution must terminate truthfully: complete successfully, recover automatically, or exit nonzero with preserved evidence after bounded recovery is exhausted. Indefinite retries and waiting for a person are not recovery. Honor explicit external stop requests and never automatically restart a deliberately stopped run.

## Assignment boundary

Edit **only** `/root/wiggum/roadmap/speckit-pipeline-skill.md` in place. This is a design assignment: the executable script, skill package, helpers, schemas, and tests described below are deliverables of the **future skill or its implementation**, not files to create now. Include their examples inside the roadmap. Do not implement or install the skill, run pipelines or model backends, provision infrastructure, or change specifications and approval records. Read-only research and isolated checks of design examples are allowed when they do not trigger those actions.

Keep the existing roadmap filename so references continue to work, but use `mixture-of-loops` for the proposed skill, package, invocation examples, and new artifact names. Preserve unrelated working-tree changes. Keep Wiggum as the execution target; another orchestrator, task router, SDK, or scheduler is outside this assignment.

## Establish the current behavior

Read the complete roadmap and example launchers, then inspect the relevant source artifacts and implementation:

- `/root/agentflow/run-005-all.sh`
- `/root/agentflow/run-wiggum-004.sh`
- `/root/agentflow/specs/005-staff-agents/`
- `/root/agentflow/specs/004-unify-agent-execution/`
- `/root/agentflow/.specify/memory/constitution.md`
- `/root/wiggum/INCIDENT-2026-08-31-ainetops-phase8.md`
- `/root/wiggum/drafts/phase-infra-preflight.sh`
- Wiggum's CLI, specification parser, verification-plan loader, backend adapters, resume implementation, and relevant tests.
- Wiggum's live-visibility and telemetry documentation, `lib/present.py`, `lib/telemetry_delivery.py`, export implementations, and bundled telemetry configuration.

Follow applicable repository instructions and record the initial working-tree status. Treat source and tests as evidence: comments, the roadmap, incident notes, and launchers can contain outdated assumptions. For material interface claims, record the source path and symbol or line, observed revision/dirty state, and whether the behavior is committed, working-tree-only, proposed, or unverified. An incident establishes what happened then; verify whether it still describes current behavior. Inspect diagnostic implementations before running them; a command called `plan`, `check`, or `--help` is not proof that it has no side effects.

Build a compact capability matrix for the interfaces the design actually needs: specification parsing, declared verification, phase boundaries, resume/stop, backend configuration, presentation, and telemetry. For each gap, choose a supported launcher mechanism, an explicitly scoped future extension, or an affected-case blocker. Do not turn a source-text match into a capability test or leave a required mechanism as “integrate with Wiggum.” If a source or official page is unavailable, record the precise uncertainty and continue the design without asserting it was verified.

## 1. Define holistic support for Spec Kit specifications

Design the skill for any supplied Spec Kit feature set within Wiggum's supported formats. Features 004 and 005 are regression cases, not templates whose paths, phase counts, providers, infrastructure, or governance rules should become universal defaults.

Discover and read the authoritative artifacts that exist, including `spec.md`, `plan.md`, `tasks.md`, `research.md`, `data-model.md`, `quickstart.md`, `contracts/`, checklists, clarifications, the repository constitution, verification declarations, and referenced local requirements that affect execution. Record which artifacts were found, absent, conflicting, or unsupported. Follow relevant references beyond the initial directory when required to understand a dependency; avoid loading unrelated repository material.

Support different project stacks, feature sizes, phase numbering, task identifiers, optional artifacts, and explicit artifact locations. Reuse Wiggum's existing parsers for the formats they support; define the semantic enrichment around them instead of inventing a competing task grammar. Read the complete relevant requirements rather than extracting only gate-command headings. If several supplied features depend on one another, derive their ordering and artifact handoffs. Distinguish a reference read for context from a feature added to execution scope; do not silently include unrelated features or authorize cross-repository work.

Account for task dependencies and `[P]` markers without assuming that a parallel marker alone makes phases, repositories, or shared-state operations safe to run concurrently. Preserve Wiggum's execution semantics and expose unsupported scheduling requirements.

Create a coverage record linking execution-relevant obligations to a pipeline stage, Wiggum task/phase, verification command, prerequisite, automated decision, existing authorization, or explicit unresolved finding. Distinguish work Wiggum implements from work the launcher must perform. Give obligations stable IDs; record their source, timing, producer/consumer, enforcement mechanism, disposition, and evidence expectation. One obligation may map to several stages or commands. Report excluded requirements with reasons, as well as missing or unresolved mappings.

The quality target is no silent loss of execution-relevant requirements within supported formats. Define coverage over an independently reviewed obligation inventory; mapping 100% of a model's own extracted list cannot prove that extraction missed nothing. Use provenance checks, deterministic validation, and behavioral fixtures to make omissions visible. Incomplete or contradictory prose must yield a reviewable draft and precise findings, not invented requirements or generic tests presented as declared gates.

## 2. Make executable script generation the core deliverable

The skill must write an executable, inspectable `run-<feature>.sh`, or an explicitly named script for a requested feature set. Include necessary prerequisite stages and feature runs. The script must be usable after generation without another model session to interpret it.

Use a machine-readable launch contract as the normalized input to deterministic validation and launcher rendering. The executable script remains the user-facing deliverable. Choose the smallest implementation that works with existing Wiggum: a helper and template are sufficient unless a shipped shared runtime has a demonstrated benefit. A future generic runner or Wiggum redesign must not be a prerequisite for useful script generation. Keep this launch contract distinct from Wiggum's verification plan and durable run state; explain their integration without replacing their ownership.

Define generated artifacts, locations, schema/version boundaries, dependencies, source-of-truth relationships, and ownership. Describe whether the launcher is self-contained or references a shipped runtime, and how referenced resources remain resolvable from another working directory or after installation. State minimum shell/helper versions and the compatibility check for the selected Wiggum installation. The contract must contain data, not executable shell configuration; construct commands from argument arrays without `eval`, and use secret references rather than embedded credentials.

Include one valid minimal contract and its matching launcher example, with the same IDs, paths, options, and stage order. State the rendering rule that connects them and which examples are runnable versus illustrative. Determinism applies to validated normalized inputs and pinned renderer versions; do not promise identical semantic judgments from separate model sessions.

Separate generation, validation, and execution. A request to generate a pipeline produces the script and supporting artifacts; it does not automatically execute that pipeline. Missing execution prerequisites must not prevent creation of a useful draft. Distinguish `draft`, `validated`, and execution readiness at a particular boundary. A syntactically executable draft must refuse a blocked execution path with actionable findings; it must not exit successfully through placeholder work. Specify behavior when parsing cannot produce a coherent stage graph at all.

## 3. Package one skill for Codex and Claude Code

Use one canonical `SKILL.md` with shared `name` and `description` frontmatter, portable instructions, and relative supporting resources. Use deterministic helpers for parsing, normalization, validation, and rendering where they materially improve reliability; reserve model judgment for semantic derivation.

Document discovery and invocation for both hosts:

- Codex: `.agents/skills/mixture-of-loops/`, invoked with `$mixture-of-loops`.
- Claude Code: `.claude/skills/mixture-of-loops/`, invoked with `/mixture-of-loops`.

Choose a canonical package location in Wiggum and show how both discovery paths reference it, including installation or linking for a separate repository such as AgentFlow. Specify helper resolution through links, behavior when the package moves or a link breaks, and how to avoid divergent copies or duplicate discovery. Keep host-specific metadata optional. Core behavior must not depend on Claude-only substitutions, hooks, or subagent APIs, or on Codex-only tool names. State the supported local-host/platform scope; do not infer cloud or Windows compatibility from local installation alone.

Keep the skill entrypoint focused; route substantial schemas, examples, and conditional procedures into supporting references. Avoid duplicated instructions or a new framework where a small helper and template suffice.

Distinguish the agent authoring the script from the proposer and critic backends selected inside the generated Wiggum invocation. Inspect Wiggum's supported backend, model, authentication, and configuration interfaces instead of assuming Codex and Claude use identical options.

Verify installation details against current official documentation and cite the supporting pages with the date checked. Distinguish documented support from behavior tested on installed host versions:

- [Codex skills and local discovery](https://learn.chatgpt.com/docs/build-skills)
- [Claude Code skills and local discovery](https://code.claude.com/docs/en/skills)

## 4. Specify derivation and prerequisite timing

Define skill inputs: target repository, feature/specification paths, output location, and optional backend, model, budget, telemetry, infrastructure, and autonomous decision/recovery policies. Infer routine values from the supplied artifacts and existing operator configuration, make defaults visible, and preserve explicit user choices. Resolve ordinary design choices autonomously. Missing execution-critical authority or irreconcilable requirements produce precise generation/preflight findings, not questions embedded in the runtime.

Classify obligations on separate axes so timing is not confused with authority:

| Axis | Required distinction |
| --- | --- |
| Kind | Implementation task, prerequisite, verification, policy/authorization decision, operational requirement |
| Timing | Before pipeline start, before/after a named stage or phase, final release |
| Producer | Existing input, earlier stage/task, automated decision policy, unavailable external authority, unknown producer |
| Disposition | Mapped, optional with rationale, unresolved, unsupported, out of scope |

A decision may be resolved from existing configuration or depend on an artifact produced later. For the latter, define the automated evaluator and acceptance policy. A missing file may be future output, not a missing launch input. Identify the earliest valid check and the boundary by which each requirement must hold; reject known autonomy-incompatible paths before starting execution.

Each derived requirement needs provenance, rationale, and timing. Include source paths, task/FR/SC identifiers where available, and source hashes or stable anchors. Treat explicit operator settings as a separate, recorded source rather than attributing them to the specification.

Do not require outputs of future work to exist before that work starts. Detect missing dependency producers and cycles. When a required Wiggum capability is absent, model an explicit prerequisite stage only if the supplied scope defines or authorizes that work; otherwise report the missing capability. Define each stage's ID, workdir/feature namespace, dependencies, preconditions, action, postconditions, evidence, and failure/resume behavior. Prefer serial dependency order for the MVP unless supported concurrency is necessary and proven safe.

For every phase-boundary check, automated decision, or recovery action, name the exact supported control point that prevents premature dependent work and advances automatically after success. A check performed after an entire `wiggum run` returns cannot protect an internal phase. Determine whether Wiggum supports the required boundary and automatic continuation while retaining gate and evidence identity. If it cannot, mark that case unsupported or specify the minimal future extension; do not simulate enforcement by watching logs, racing a stop request, or splitting specifications in a way that changes gate meaning.

Unresolved execution-critical findings block the affected execution path while allowing independent generation and validation. Record automated decisions with policy version, actor, input/artifact hashes, result, and evidence. Validate existing authorization against its actual scope and identity requirements, and invalidate decisions when relevant inputs change. Never invent identities, signatures, budget authority, or deployment scope. An automated critic approval is valid where the specification permits it; it must not be misrepresented as a human signature.

## 5. Derive verification commands accurately

Define behavior when `verification-commands.json` exists, is missing, or disagrees with `plan.md` or `tasks.md`. Preserve an existing file as authored input and validate it against the other declarations. If missing, derive a reviewable candidate only from actual declared commands; if no declaration exists, report that absence. Specify how deterministic validation and existing authorization make a candidate accepted input without a manual sign-off step. Resolve conflicts only through an evidenced authority rule; otherwise report the blocker before launch. File format or recency alone must not silently decide authority.

Preserve declared command order, identity, executable/argv boundaries, cwd, environment, timeout, and phase mapping. Wiggum's current verification interface uses `executable` plus `args`; it is not an arbitrary shell-string interface. Unsupported shell expressions must become explicit findings or an explicitly specified, supported representation, not silently altered commands. Preserve source command text alongside the parsed form so quoting and shell semantics can be reviewed. Check declared nonzero-exit exceptions, non-gating measurements, phase gates, and cumulative release gates against actual runtime support; do not invent exit-code policies from comments.

Account for differences between `tasks.md` phase numbers and `plan.md` delivery phases; feature 005 already includes a `phase_map`. Inspect whether that map is consumed by Wiggum or merely documents a relationship. Require a reconciliation table from source phase/task to declared command ID to the actual Wiggum phase/gate, with counts that expose omissions, collisions, and unintended duplication. Inspect when executable and cwd validation occurs: a directory or tool created by a future stage must not be assumed available during generation or early verification planning.

Define treatment of phases without declared commands without inventing a gate or replacing a declared gate with discovered generic tests. Distinguish supplemental discovered tests from required declared commands and preserve both according to Wiggum's verified semantics. Bind derivation to all relevant source content, including dirty and untracked inputs, rather than only to the current commit. Separate raw provenance hashes from semantic staleness checks: inspect Wiggum's treatment of completed task checkboxes so ordinary progress does not invalidate the launcher. Requirements, command, dependency, and authorization changes must trigger policy-governed revalidation or a nonzero stale-input failure, never an interactive review wait.

## 6. Define generated-script controls and lifecycle

Use **`--implement` instead of `--provision`** in the proposed generated-script interface, help, examples, and acceptance criteria. Here `--implement` is the renamed opt-in control for setting up declared prerequisites: bring up or prepare the specified infrastructure/services, verify their postconditions, then proceed through the normal pipeline. It does not imply permission to invent approvals or perform unrelated deployment work.

Preserve the normal launcher behavior: execution uses existing prerequisites by default; `--implement` permits the declared setup actions before execution; `--dry-run` performs inspection only. Feature implementation still runs in normal execution mode without `--implement`. Classify each prerequisite as an existing input, setup action controlled by `--implement`, or an explicitly scoped implementation stage; explain its behavior in each mode. The existing example's `--provision` spelling may be mentioned as history, but must not be advertised as the new interface.

Specify:

- Explicit configuration, validated options, and one precedence table covering CLI, environment, contract defaults, and Wiggum's configuration loading. Record effective non-secret settings and pass them through both `run` and `resume` using their actual interfaces. Audit `last-run.conf` against model, timeout, wall-budget, and backend environment controls; persist and restore settings it omits, with secret references resolved at execution time. Bare `wiggum resume` must not be assumed to reproduce the full configuration.
- Path resolution without universal `/root` assumptions; correct handling of spaces and argument quoting.
- Aggregate preflight diagnostics with phase/stage attribution and concrete remediation.
- Schema and value checks where required, beyond file and environment-variable presence.
- Dependency stages with verifiable postconditions and capability-based skipping.
- `--implement` setup actions that are repeatable and scoped to the declared resources.
- Optional smoke checks whose exact backend/model coverage is accurately described.
- Bounded automatic budget resumes based on verified Wiggum exit semantics and persisted stop reason; distinguish per-attempt limits from a total pipeline budget. Inspect structured `run_stop.reason` and whether exit `4` also covers iteration exhaustion, repeated proposer errors, or watchdog stalls; do not copy the example's unconditional exit-4 resume rule. A generic failure, explicit stop, signal, or ambiguous result must not become an automatic budget retry.
- Preservation of intentional stop requests, live-run ownership, and existing evidence.
- Automated phase and final acceptance decisions, authorized release actions, signal handling, and exit-code propagation.
- Source-change detection, reviewable regeneration, and preservation of user-edited generated files. Define generated-file markers/hashes, an explicit override location, and atomic publication of matching script/contract versions; interrupted regeneration must leave the previous valid pair intact.
- A final digest on success, failure, or interruption with the last stage, exit reason, evidence paths, and the appropriate next action.

Define the lifecycle as observable states and transitions, including preflight, ready, running, recovering, stopped, failed, and completed; a preflight blocker exits nonzero, and there is no waiting-for-human state. Distinguish resuming a Wiggum feature from resuming a multi-stage launcher: after recoverable interruption, revalidate completed-stage postconditions and retain supported Wiggum evidence/ownership semantics. Define automatic recovery by failure class, allowed action, backoff, attempt limit, total deadline/budget, and terminal outcome. If recovery from launcher-process death requires an external supervisor, name that dependency and how it is configured; a dead process cannot restart itself. Do not create a second gate authority or launch over a live run. Specify launcher exit codes separately from child exit codes, including what wins if logging or cleanup also fails.

Inspect all subprocesses for unattended operation, including backend permission prompts, package installers, authentication, setup tools, and gate commands. Use supported noninteractive modes within existing authorization, bounded timeouts, and controlled stdin; do not answer unknown prompts with a blanket `yes`. Missing credentials or required permissions must fail preflight. No success path may require an operator to run a printed follow-up command.

`--dry-run` must take precedence over `--implement` and smoke options regardless of argument order. It must not provision services, call a model, mutate run state, create output/log directories, clear stop/PID files, or execute feature/gate commands. Clearly distinguish permitted read-only diagnostics from actions requiring execution mode. Parse modes before initialization, traps that write files, or side-effecting configuration loading. Provide a mode/action table for default execution, `--implement`, `--dry-run`, and smoke combinations, including stdout/stderr and exit behavior. Generation's authorized file writes are separate from the generated script's dry-run behavior.

Investigate the example's weaknesses and replace them in the design: broad repository text search for capability detection, accepting any kind cluster, presence-only input checks, deployment-specific backend checks, and clearing state during preflight. Do not copy these behaviors into the skill as defaults.

## 7. Provide full colored observability

Color both launcher-level messages and the live Wiggum view. Define stable textual labels plus a consistent palette, for example: cyan for stages and progress, green for success, yellow for warnings or retries, red for errors/rejections, magenta for automated decisions and recovery, and dim text for timestamps and metadata. Meaning must remain readable without color.

Expose automatic terminal color detection and an explicit `--color=auto|always|never` control, with a documented `--no-color` alias if useful. Honor `NO_COLOR` and unsuitable terminals; document option precedence. These are launcher controls: map them to Wiggum's actual supported controls rather than forwarding invented flags. Keep machine-readable events, telemetry payloads, and durable plain-text logs free of ANSI escapes.

Reuse Wiggum's existing structured event stream and presenter. Verify how to expose `--live`, `WIGGUM_AGENT_STREAM=true`, and `WIGGUM_LIVE_DETAIL=full` across the supported backends and resumes. Show stage/phase transitions, proposer and critic activity, available tool events, verification outcomes, automated decisions/recovery, external stops, and available cost/token/duration totals. Mark unavailable or degraded data accurately rather than synthesizing it.

Address the terminal/logging interaction explicitly: a blanket `exec > >(tee ...)` can make Wiggum see a non-TTY and suppress its colors. Choose a supported arrangement that preserves the colored terminal view and plain persistent logs, keeps child exit status and signals intact, and avoids duplicate presenters or duplicated event output. Show the process and file-descriptor flow, presenter ownership, and log destinations for foreground and detached execution. Specify behavior when the presenter fails or a consumer is slow. Support the choice with source evidence and a concrete TTY test design; do not claim colored observability based only on ANSI launcher headings.

Expose Wiggum's independent exports:

- Loki: `--telemetry` with its own `--loki-url`.
- OpenTelemetry: `--otel` with its own `--otel-url`.

Make full observability a first-class generated configuration: display the selected live-detail mode, sink enablement, resolved non-secret endpoints, and available dashboard/inspection links. Derive endpoints from the target deployment or explicit operator configuration. Do not confuse Loki and OTLP ports or assume AgentFlow's stack is universal. Carry the selected exports through every applicable pipeline stage and resume.

Where the target configuration supplies both sinks, support enabling both. Where it does not, expose the missing setup/configuration and an explicit local-only option; do not label local-only execution as successful remote delivery. Any required telemetry setup belongs to declared `--implement` actions. Use source requirements and operator choices to determine whether a failed sink blocks execution or is a reported degradation.

Surface Wiggum's actual delivery states: configured, reachable, request-accepted, and query-verified. A listening port does not prove delivery. Report the highest demonstrated state separately for each sink, including evidence time and run correlation so an old successful delivery cannot represent the current run. Distinguish the OTLP ingestion endpoint from any backend query/dashboard endpoint; describe query verification only where it is actually supported. Inspect which logs, metrics, or traces the implementation exports before describing support. Keep telemetry status current during execution and include it in the closing digest.

Include correct inspection commands for the relevant workdir and feature: status, watch, events, raw logs, feedback, and evidence. For backgrounded runs, explain how an operator can attach to the colored view. Preserve available stage/run/feature correlation and avoid replaying unrelated historical events. Do not expose credentials through endpoint display, command logging, or environment summaries.

## 8. Add an implementation plan and behavioral acceptance criteria

Provide a minimal package layout, concrete output examples, implementation milestones, and measurable acceptance criteria. Include a normal small feature example and a larger pipeline example so holistic support is demonstrated without making every launcher as complex as feature 005. For the larger example, trace representative obligations end to end through source, contract, stage, command or automated decision, and evidence. Show at least one generated prerequisite, one automatic recovery, and one blocked finding, not only the happy path.

Walk the design against the available artifacts now; specify the following behavioral checks for the later implementation. Use a matrix with scenario, fixture/setup, expected observable result, and validation status (`source-reviewed`, `checked now`, `planned`, or `blocked`):

- Feature 005: independently derive obligations, prerequisite stages, phase mappings, infrastructure, and verification commands. Reconcile all 12 `REQUIRED_INPUTS`: distinguish specifications, machine-generated artifacts, existing authorizations, and actual non-delegable decisions. Audit the historical US6 and release stops explicitly: automate them where authorized or identify the exact prelaunch incompatibility. Reproducing those manual stops fails this assignment.
- Feature 004: recover omitted declared gates without inheriting unrelated 005 infrastructure.
- Additional representative Spec Kit inputs with different stacks, phase structures, optional artifacts, custom paths, and explicitly linked features. Avoid tailoring a parser solely to the two known fixtures.
- Ambiguous approvals, missing/conflicting commands, unsupported formats, generated prerequisites, dependency cycles, and changed source artifacts. Verify that blockers identify the source and prevent the affected launch; checkbox-only progress must not be confused with changed requirements.
- Equivalent observable behavior when the skill is used by Codex or Claude Code, including discovery and relative-resource resolution. Distinguish tested compatibility from compatibility inferred from packaging.
- End-to-end unattended completion with stdin closed and no TTY, including generated prerequisites, automated decisions, transient failure recovery, bounded resume, and final acceptance. Assert that no subprocess waits for input and no manual follow-up is required for success. An unexpected interactive requirement must cause bounded failure rather than a hung run.
- Non-delegable external approval discovered during derivation, missing credentials/permissions at preflight, and exhausted recovery budgets: no affected feature/model run starts in the preflight cases; terminal failures preserve evidence and never fabricate approval or report completion.
- Shell syntax/static checks and stubbed execution checks for stage ordering, argument preservation (including spaces and shell metacharacters), configuration precedence, exit propagation, bounded resume, and interruption. Include fresh launch, interrupted setup, existing live run, intentional stop, repeated invocation, and edited generated artifacts.
- `--implement` idempotence and postcondition checking, plus dry-run precedence in every option order. Assert no setup/model calls, state deletion, or directory/log creation during dry-run.
- TTY and redirected-output cases: colored launcher and Wiggum activity in the terminal; plain logs and valid JSONL/telemetry; `NO_COLOR` and explicit color settings; no duplicate presenters.
- Independent Loki and OTLP endpoint wiring, propagation through stages and resume, truthful degraded-delivery reporting, and a final digest even when a stage fails.

Keep validation isolated from actual model runs and infrastructure provisioning; propose fixtures and stubbed services/commands where execution would otherwise have external effects. Tests should verify observable behavior and coverage rather than matching generated prose or copying the implementation. Derive fixture expectations independently of the renderer and the historical launchers, which contain known mistakes. Do not describe planned tests as passed, or stubbed host compatibility as a real Codex/Claude Code execution test.

## Deliverable

Produce a cohesive enhanced roadmap that contains:

1. The problem and preserved provenance/gate-integrity argument, with historical claims corrected or qualified.
2. A current capability matrix and a chosen MVP architecture, including a compact data/process-flow diagram.
3. The canonical package layout, host discovery/invocation, minimum dependencies, and one concise `SKILL.md` sketch with frontmatter and references.
4. The derivation workflow, source/coverage contract, dependency and automated-decision timing, and verification reconciliation rules.
5. A matching minimal contract/launcher example and a larger worked derivation, with blocked cases explicit.
6. Configuration and mode tables, fully unattended lifecycle/recovery/resume behavior, regeneration rules, and the colored terminal/log/telemetry design.
7. Ordered implementation milestones and the acceptance matrix, with evidence for checks performed now separated from planned validation.
8. MVP limits, future enhancements, and true blockers, each naming its affected behavior and what resolves it.

Resolve routine design questions with reasoned recommendations; convert the existing roadmap's open questions into decisions, deferred work, or evidenced blockers. Preserve its provenance and gate-integrity argument while replacing the assumption of manual pipeline checkpoints with fully autonomous execution. Keep requirements in one place and cross-reference them rather than repeating this prompt. Every required behavior must have either a concrete enforcement mechanism and acceptance check or an explicit unsupported-case boundary. The result should let another agent implement the MVP without rediscovering its contracts or mistaking proposals for existing capabilities.

Before finishing, review the roadmap against this deliverable list, reconcile its examples with the inspected interfaces, and inspect the diff to confirm the authorized edit scope. Finish with a concise summary of what changed in the roadmap, the main architectural decision, checks actually performed, and any remaining implementation blockers.
