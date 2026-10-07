---
name: specstride-batch
description: Run the Specstride / Mixture of Loops pipeline over several Spec Kit features at once — derive one MoL contract per feature and launch it, sequentially or N features in parallel, on claude (Claude Code) or dsh (DeepSeek Harness), with the model and reasoning effort pinned per run, a shared queue so two harnesses drain batches side by side, and background tracking that reports per-feature results. Use this whenever the user wants to derive, launch, or otherwise process a range, list, or batch of spec folders through Specstride or mixture-of-loops ("derive contracts for 105-107 with fable", "run the MoL pipeline on 108-111 on dsh with glm-5.3", "batch the remaining specs 4 features at a time through specstride", "launch the next specstride batch when this one ends"), whenever a Specstride derivation must run on dsh with a specific model, and whenever more than one spec needs the same Specstride pipeline — even if the word "batch" is never said.
---

# specstride-batch

Run one Specstride (Mixture of Loops) pass across many `specs/<id>-*` folders, on either
harness, with the model and reasoning bound per run. Everything mechanical lives in two
scripts under `scripts/`; your job is to pick the parameters, launch, track, and report.
The scripts follow the proven `speckit-batch` design (same selectors, same status format,
same quota handling); the difference is the command they run — the `mixture-of-loops`
pipeline (derive the contract, then launch it) instead of a single `speckit-*` analysis.

## What the user usually means

| They say | You run |
|---|---|
| "derive 105-107 with claude/fable" | `--harness claude --model fable 105-107` |
| "108 to 111 on dsh, glm-5.3, max reasoning" | `--harness dsh --model glm-5.3 --reasoning max 108-111` |
| "run 3 at a time" | add `--jobs 3` |
| "next batch when this one ends", "batches of 4-5" | `chain.sh` with a queue file (below) |
| "only derive, don't launch" | `--mode derive` |
| "only launch, the contracts exist" | `--mode launch` |

Spec numbering has gaps (no 114, 120, 121, 132, ...), so selectors are flexible and mixable:
`105-107` (range), `122,123,124` (list), `113` (one id), `130-adapter-dsh` (folder name).
Missing ids inside a range are logged as `SKIP`, never an error. Duplicates run once.

## Launch

```bash
S=~/.claude/skills/specstride-batch/scripts
cd <spec-kit-repo>            # or pass --repo; it must hold specs/ and .specify/

# Claude Code — model alias or id, effort low|medium|high|xhigh|max
nohup $S/run-batch.sh --harness claude --model fable --reasoning high \
  105-107 > /dev/null 2>&1 &

# dsh — model id under a declared provider (glm-* -> zai, qwen* -> local-high, else --provider)
nohup $S/run-batch.sh --harness dsh --model glm-5.3 --reasoning max --jobs 2 108-111 > /dev/null 2>&1 &
```

Always `--dry-run` first: it prints the resolved feature list and the exact prompt, exits
non-zero on a bad selector or an undeclared dsh provider, and writes nothing. Run each
harness from `nohup … &` (or Bash `run_in_background`) — a feature takes 10–60 min.

The prompt per feature names the `mixture-of-loops` skill and the mode tail (see
`DEFAULT_PROMPT_TAIL` in `run-batch.sh`): derive the MoL contract for exactly this feature
from its spec/plan/tasks, validate it, and — unless `--mode derive` — launch the run and
supervise it to an end (done / failed / parked), reporting the end state. `--prompt-tail`
overrides the tail for a custom pass. For a read-only pass: `--prompt-tail "derive only,
launch nothing, report the contract summary"`.

Logs land in `<repo>/../specstride-batch-runs/logs/` (override with `--logdir`):
`<harness>.status` (append-only: `RANGE`, `START`, `END rc=N`, `SKIP`, `ALL-DONE`) and
`<harness>-<feature>.log` (the agent's final message plus stderr).

## Queues and chaining

```bash
printf '%s\n' "112-117" "118-124" "125-129" > queue.txt
nohup $S/chain.sh --queue queue.txt -- --harness claude --model fable --reasoning high &
nohup $S/chain.sh --queue queue.txt -- --harness dsh --model glm-5.3 --reasoning max  &
```

Claims are atomic under `flock`, so two harnesses can drain one queue without doubling a
batch. On a provider quota stop (`exit 75`) the unfinished features go back to the front of
the queue and the chain sleeps until the reset time named in the error (`--on-quota stop`
ends the chain instead). A pool of N parallel slots = N chains with `--jobs 1` on a queue
of one feature per line.

## Campaign safety

A feature the Specstride campaign is implementing right now (listed in
`.mixture-of-loops/campaign/active.json`) is skipped, never double-run. A feature that
already has a live MoL run in its own `.mixture-of-loops/` is reported so you can decide,
not silently overwritten.
