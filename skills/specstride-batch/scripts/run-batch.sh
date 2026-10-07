#!/usr/bin/env bash
# run-batch.sh — run the Specstride / Mixture of Loops pipeline over a set of
# specs/<id>-* folders on claude (Claude Code) or dsh (DeepSeek Harness);
# sequentially, or --jobs N side by side.
#
#   run-batch.sh --harness claude --model fable   --reasoning high 105-107
#   run-batch.sh --harness dsh    --model glm-5.3 --reasoning max  108-111 113 115-117
#   run-batch.sh --harness dsh    --model glm-5.3 --jobs 3         122,123,124 130-reverse
#
# Spec selectors (any number, mixed): "105-107" a numeric range, "122,123" a list,
# "113" one id, "130-reverse" a folder name. Ids without a specs/<id>-* folder are
# reported as SKIP (the numbering has gaps: 114, 120, 121, ...). Duplicates run once.
#
# Options (env var fallback in parentheses):
#   --harness claude|dsh      (HARNESS)   required
#   --model <name>            (MODEL)     claude: alias or full id; dsh: model id under a provider
#   --provider <name>         (PROVIDER)  dsh only; default zai for glm-*, local-high for qwen*
#   --reasoning <level>       (REASONING) claude --effort: low|medium|high|xhigh|max
#                                         dsh reasoningEffort: off|low|medium|high|max (as the provider declares)
#   --mode <m>                (MODE)      full (default): derive the MoL contract, then launch and
#                                         supervise the run to an end; derive: contract only, launch
#                                         nothing; launch: run the existing contract, derive nothing
#   --prompt-tail "<text>"    (PROMPT_TAIL) appended after the mode instruction; default = the mode
#                                         tail (see DEFAULT_PROMPT_TAIL below)
#   --repo <dir>              (REPO)      Spec Kit repo, default $PWD; must hold specs/ and .specify/
#   --logdir <dir>            (LOGDIR)    default <repo>/../specstride-batch-runs/logs
#   --settings <file>         (CLAUDE_SETTINGS) claude --settings file (optional)
#   --jobs <n>                (JOBS)      features run side by side within the batch, default 1
#   --dry-run                             print the command per feature and exit
#
# Per feature: <logdir>/<harness>-<feature>.log  (agent's final output + stderr)
# Status:      <logdir>/<harness>.status          (RANGE, START, END rc=N, SKIP, ALL-DONE lines;
#              on a provider quota error: END … QUOTA, ABORTED, exit 75, and the unfinished
#              features in <logdir>/<harness>.requeue)
set -uo pipefail

HARNESS="${HARNESS:-}"; MODEL="${MODEL:-}"; PROVIDER="${PROVIDER:-}"; REASONING="${REASONING:-}"
MODE="${MODE:-full}"
set_mode_tail() {
case "$MODE" in
  full)
    DEFAULT_PROMPT_TAIL='Derive the Mixture of Loops contract for exactly this feature from its spec.md, plan.md and tasks.md, validate it, then launch the run and supervise it to an end (done, failed, or parked). Report the end state, the contract path, and any parked reason in one short block.'
    ;;
  derive)
    DEFAULT_PROMPT_TAIL='Derive the Mixture of Loops contract for exactly this feature from its spec.md, plan.md and tasks.md and validate it; launch nothing. Report the contract path and a one-line summary of its loops and gates.'
    ;;
  launch)
    DEFAULT_PROMPT_TAIL='The Mixture of Loops contract for exactly this feature already exists in its .mixture-of-loops/ directory: validate it, launch the run and supervise it to an end (done, failed, or parked). Do not re-derive. Report the end state and any parked reason in one short block.'
    ;;
  *) echo "run-batch.sh: --mode must be full, derive, or launch" >&2; exit 2 ;;
esac
}
REPO="${REPO:-$PWD}"; LOGDIR="${LOGDIR:-}"; CLAUDE_SETTINGS="${CLAUDE_SETTINGS:-}"; JOBS="${JOBS:-1}"; DRY_RUN=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --harness) HARNESS="$2"; shift 2 ;;
    --model) MODEL="$2"; shift 2 ;;
    --provider) PROVIDER="$2"; shift 2 ;;
    --reasoning) REASONING="$2"; shift 2 ;;
    --mode) MODE="$2"; shift 2 ;;
    --prompt-tail) PROMPT_TAIL="$2"; shift 2 ;;
    --repo) REPO="$2"; shift 2 ;;
    --logdir) LOGDIR="$2"; shift 2 ;;
    --settings) CLAUDE_SETTINGS="$2"; shift 2 ;;
    --jobs) JOBS="$2"; shift 2 ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) sed -n '2,31p' "$0"; exit 0 ;;
    --*) echo "run-batch.sh: unknown option $1" >&2; exit 2 ;;
    *) break ;;
  esac
done
[[ $# -gt 0 ]] || { echo "usage: run-batch.sh --harness claude|dsh --model <m> [--reasoning <r>] [--mode full|derive|launch] [--jobs n] <spec selectors...>" >&2; exit 2; }
SELECTORS=("$@")
set_mode_tail
PROMPT_TAIL="${PROMPT_TAIL:-$DEFAULT_PROMPT_TAIL}"
PROMPT_TAIL="${PROMPT_TAIL:-$DEFAULT_PROMPT_TAIL}"
[[ "$HARNESS" == claude || "$HARNESS" == dsh ]] || { echo "run-batch.sh: --harness must be claude or dsh" >&2; exit 2; }
[[ -n "$MODEL" ]] || { echo "run-batch.sh: --model is required" >&2; exit 2; }
[[ "$JOBS" =~ ^[1-9][0-9]*$ ]] || { echo "run-batch.sh: --jobs must be a positive integer" >&2; exit 2; }
[[ -d "$REPO/specs" && -d "$REPO/.specify" ]] || { echo "run-batch.sh: $REPO has no specs/ and .specify/ (not a Spec Kit repo)" >&2; exit 2; }
REPO="$(cd "$REPO" && pwd)"

# ── expand the selectors into an ordered, de-duplicated feature list ─────────
FEATURES=(); SKIPPED=()
add_id() {  # numeric id -> its specs/<id>-* folder, or a SKIP note
  local dir; dir="$(ls -d "$REPO/specs/$1"-* 2>/dev/null | head -1)"
  if [[ -n "$dir" ]]; then add_feat "$(basename "$dir")"; else SKIPPED+=("$1"); fi
}
add_feat() {
  local f
  for f in "${FEATURES[@]}"; do [[ "$f" == "$1" ]] && return; done
  FEATURES+=("$1")
}
for sel in "${SELECTORS[@]}"; do
  IFS=',' read -ra parts <<< "$sel"
  for part in "${parts[@]}"; do
    [[ -n "$part" ]] || continue
    if [[ "$part" =~ ^([0-9]+)-([0-9]+)$ ]]; then
      for ((i=BASH_REMATCH[1]; i<=BASH_REMATCH[2]; i++)); do add_id "$i"; done
    elif [[ "$part" =~ ^[0-9]+$ ]]; then
      add_id "$part"
    elif [[ -d "$REPO/specs/$part" ]]; then
      add_feat "$part"
    else
      echo "run-batch.sh: '$part' is neither an id, a range, nor a folder under $REPO/specs" >&2; exit 2
    fi
  done
done
[[ ${#FEATURES[@]} -gt 0 ]] || { echo "run-batch.sh: no existing spec folder matched ${SELECTORS[*]}" >&2; exit 2; }
LABEL="${SELECTORS[*]}"
LOGDIR="${LOGDIR:-$(dirname "$REPO")/specstride-batch-runs/logs}"
(( DRY_RUN )) || mkdir -p "$LOGDIR"
status="$LOGDIR/$HARNESS.status"
(( DRY_RUN )) && status=/dev/null   # a dry run leaves no trace

# ── dsh: bind the model through a throwaway DSH_HOME ────────────────────────
# dsh has no per-run model flag, and `--patch` on agent-default-model composes into
# the profile tree but loses to $DSH_HOME/settings.yaml at run time. So: a temp home
# that symlinks every entry of the real one except settings.yaml, rewritten with the
# chosen agent-default-model block. Nothing global changes; concurrent dsh users on
# the host are unaffected. (Recipe from specstride's proposer.sh, verified 2026-09-30.)
OVERLAY=""
if [[ "$HARNESS" == dsh ]]; then
  if [[ -z "$PROVIDER" ]]; then
    case "$MODEL" in
      */*) PROVIDER="${MODEL%%/*}"; MODEL="${MODEL#*/}" ;;
      glm-*) PROVIDER=zai ;;
      qwen*|nemotron*|muse*) PROVIDER=local-high ;;
      *) echo "run-batch.sh: dsh model '$MODEL' needs --provider (see llm-pi-ai.providers in ~/.dsh/settings.yaml)" >&2; exit 2 ;;
    esac
  fi
  REAL_HOME="${DSH_HOME:-$HOME/.dsh}"
  if ! grep -q "^    $PROVIDER:" "$REAL_HOME/settings.yaml" 2>/dev/null; then
    echo "run-batch.sh: provider '$PROVIDER' is not declared under llm-pi-ai.providers in $REAL_HOME/settings.yaml" >&2; exit 2
  fi
  if (( ! DRY_RUN )); then
    OVERLAY="$(mktemp -d "$(dirname "$LOGDIR")/dsh-home.XXXXXX")"
    trap 'wait; rm -rf "$OVERLAY"' EXIT
    for entry in "$REAL_HOME"/* "$REAL_HOME"/.[!.]*; do
      [[ -e "$entry" ]] || continue
      base="${entry##*/}"
      [[ "$base" == settings.yaml ]] && continue
      ln -sfn "$entry" "$OVERLAY/$base"
    done
    {
      printf 'agent-default-model:\n  provider: %s\n  model: %s\n' "$PROVIDER" "$MODEL"
      [[ -n "$REASONING" ]] && printf '  reasoningEffort: %s\n' "$REASONING"
      awk '/^agent-default-model:/ { skip=1; next }
           skip && /^[[:space:]]*$/ { next }
           skip && /^[[:space:]]/   { next }
           { skip=0; print }' "$REAL_HOME/settings.yaml"
    } > "$OVERLAY/settings.yaml"
    export DSH_HOME="$OVERLAY"
    export DSH_PERMISSION_MODE=danger-full-access   # dsh's --dangerously-skip-permissions
  fi
fi

run_one() {  # $1 = feature folder name; runs from the repo root, feature pinned by env
  local feat="$1"
  # SPECIFY_FEATURE* pin the feature for .specify/scripts/bash/check-prerequisites.sh,
  # which otherwise resolves it from the shared .specify/feature.json — a race when
  # two batches run side by side in the same repo.
  export SPECIFY_FEATURE="$feat" SPECIFY_FEATURE_DIRECTORY="specs/$feat"
  case "$HARNESS" in
    claude)
      local args=(--model "$MODEL" --dangerously-skip-permissions)
      [[ -n "$REASONING" ]] && args+=(--effort "$REASONING")
      [[ -n "$CLAUDE_SETTINGS" ]] && args+=(--settings "$CLAUDE_SETTINGS")
      # Every run gets a session id of its own, kept in <logdir>/claude-<feature>.session
      # while the feature is unfinished. After a quota stop the next attempt resumes that
      # session (what it read and derived so far is in its transcript) instead of paying
      # for the whole run again; a session that cannot be resumed starts over.
      # IS_SANDBOX=1 lets --dangerously-skip-permissions run as root
      local sidf="$LOGDIR/$HARNESS-$feat.session" out rc
      if [[ -s "$sidf" ]]; then
        out="$(IS_SANDBOX=1 claude "${args[@]}" --resume "$(cat "$sidf")" -p "$RESUME_PROMPT" 2>&1)"; rc=$?
        printf '%s\n' "$out"
        if (( rc == 0 )) || grep -qiE "$QUOTA_RE" <<< "$out"; then return "$rc"; fi
        echo "[run-batch] the interrupted session could not be resumed (rc=$rc); starting $feat over"
      fi
      cat /proc/sys/kernel/random/uuid > "$sidf"
      IS_SANDBOX=1 claude "${args[@]}" --session-id "$(cat "$sidf")" \
        -p "Use the mixture-of-loops skill for the feature $feat. $PROMPT_TAIL" ;;
    dsh)
      # The headless profile has no slash-command adapter: name the skill in prose
      # first, then the instruction; the model reaches the skill via its `skill` tool
      # (skills live in $DSH_HOME/skills and the repo's .dsh/skills).
      dsh --profile headless "Use the mixture-of-loops skill for the feature $feat. $PROMPT_TAIL" ;;
  esac
}

# A provider quota / rate-limit failure is not a pipeline result: the feature must run
# again later, and starting more features now only burns the batch. run_feature leaves a
# marker the launch loop checks; the batch then stops, writes the unfinished features to
# <logdir>/<harness>.requeue and exits 75 (EX_TEMPFAIL) so chain.sh can re-queue and wait.
# Claude Code words it "You've hit your session limit · resets 9pm (Asia/Dubai)" (also
# "weekly limit", "usage limit") or, for a per-model cap with no reset time, "You've reached
# your Fable limit. Switch to another model…"; z.ai: "429 … Usage limit reached … reset at <date>".
QUOTA_RE='QUOTA|429|rate.?limit|usage limit|hit your ([a-z0-9. -]+ )?limit|reached your ([a-z0-9. -]+ )?limit|limit will reset'
RESET_RE='reset at [0-9: -]+|resets [^()]+\([^)]+\)'
# what a claude session hears when it is resumed after a quota stop
DEFAULT_RESUME_PROMPT='A usage limit of the provider interrupted this run. Continue from where you stopped; do not start the work over: finish what is left under the same rules as before, then give the closing output exactly as first instructed.'
RESUME_PROMPT="${RESUME_PROMPT:-$DEFAULT_RESUME_PROMPT}"
QUOTA_MARK="$LOGDIR/$HARNESS.quota.$$"
REQUEUE="${REQUEUE_FILE:-$LOGDIR/$HARNESS.requeue}"   # chain.sh passes its own file: chains sharing a logdir must not share it
rm -f "$QUOTA_MARK"

run_feature() {  # $1 = feature; one full START..END cycle, safe to run in the background
  local feat="$1" log="$LOGDIR/$HARNESS-$feat.log" rc
  echo "$(date -Is) START $feat" >> "$status"
  [[ -s "$log" ]] && mv "$log" "$log.$(date -r "$log" +%Y%m%dT%H%M%S)"   # keep the previous run's result
  ( cd "$REPO" && run_one "$feat" ) > "$log" 2>&1
  rc=$?
  if (( rc != 0 )) && grep -qiE "$QUOTA_RE" "$log"; then
    echo "$feat" >> "$QUOTA_MARK"
    echo "$(date -Is) END $feat rc=$rc QUOTA $(grep -oiE "$RESET_RE" "$log" | head -1)" >> "$status"
  else
    rm -f "$LOGDIR/$HARNESS-$feat.session"   # finished (or failed for good): nothing to resume
    echo "$(date -Is) END $feat rc=$rc" >> "$status"
  fi
}

echo "$(date -Is) RANGE $LABEL harness=$HARNESS mode=$MODE model=${PROVIDER:+$PROVIDER/}$MODEL reasoning=${REASONING:-default} jobs=$JOBS pid=$$${OVERLAY:+ home=$OVERLAY}" >> "$status"
# Up to $JOBS features run side by side. This is safe because every run is pinned to
# its own feature through SPECIFY_FEATURE* (the shared .specify/feature.json is only a
# fallback) and each writes only inside specs/<feature>/. Keep it modest: each run is a
# full agent session, so the ceiling is the provider's rate limit, not the CPU.
for id in "${SKIPPED[@]}"; do echo "$(date -Is) SKIP $id (no specs/$id-* folder)" >> "$status"; done
# A feature the Specstride campaign is implementing right now is listed in the campaign's
# active.json; deriving and launching a second loop on it turns the campaign's checks red.
# Skip it.
campaign_active() {  # $1 = feature folder name
  local f="$REPO/.mixture-of-loops/campaign/active.json"
  [[ -s "$f" ]] || return 1
  python3 -c 'import json,sys; d=json.load(open(sys.argv[1])); sys.exit(0 if any(r.get("feature")==sys.argv[2] for r in d.get("running",[])) else 1)' "$f" "$1" 2>/dev/null
}
live_contract() {  # $1 = feature; 0 when a MoL run is already live in the feature's own dir
  [[ "$MODE" == derive ]] && return 1
  [[ -f "$REPO/specs/$1/.mixture-of-loops/run.pid" ]] && kill -0 "$(cat "$REPO/specs/$1/.mixture-of-loops/run.pid")" 2>/dev/null
}
NOT_STARTED=()
for feat in "${FEATURES[@]}"; do
  if campaign_active "$feat"; then
    echo "$(date -Is) SKIP $feat (running in the Specstride campaign, active.json)" >> "$status"
    (( DRY_RUN )) && echo "[$HARNESS] $feat: SKIP (running in the Specstride campaign, active.json)"
    continue
  fi
  if live_contract "$feat"; then
    echo "$(date -Is) SKIP $feat (a MoL run is already live in specs/$feat/.mixture-of-loops)" >> "$status"
    (( DRY_RUN )) && echo "[$HARNESS] $feat: SKIP (a MoL run is already live)"
    continue
  fi
  if (( DRY_RUN )); then
    echo "[$HARNESS ${PROVIDER:+$PROVIDER/}$MODEL${REASONING:+ @$REASONING} x$JOBS mode=$MODE] $feat: mixture-of-loops — $PROMPT_TAIL"
    continue
  fi
  if [[ -e "$QUOTA_MARK" ]]; then NOT_STARTED+=("$feat"); continue; fi
  if (( JOBS > 1 )); then
    while (( $(jobs -rp | wc -l) >= JOBS )); do wait -n; done
    run_feature "$feat" &
  else
    run_feature "$feat"
  fi
done
wait
(( DRY_RUN )) && exit 0
if [[ -e "$QUOTA_MARK" ]]; then
  # unfinished = the features the quota cut off (in order) + the ones never started
  { cat "$QUOTA_MARK"; printf '%s\n' "${NOT_STARTED[@]}"; } | awk 'NF && !seen[$0]++' | tr '\n' ' ' | sed 's/ $/\n/' > "$REQUEUE"
  reset="$(for f in $(cat "$QUOTA_MARK"); do grep -ohiE "$RESET_RE" "$LOGDIR/$HARNESS-$f.log"; done 2>/dev/null | tail -1)"
  echo "$(date -Is) ABORTED $LABEL quota — unfinished: $(cat "$REQUEUE") ${reset:+($reset)}" >> "$status"
  rm -f "$QUOTA_MARK"
  exit 75
fi
echo "$(date -Is) ALL-DONE $LABEL" >> "$status"
