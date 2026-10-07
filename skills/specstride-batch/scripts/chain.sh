#!/usr/bin/env bash
# chain.sh — keep one harness busy: claim the next batch from a shared queue and run it
# with run-batch.sh, until the queue is empty. Several chains (e.g. one claude, one dsh)
# may share a queue; claims are atomic under flock, so no batch runs twice.
#
#   chain.sh --queue <file> [--wait-pid <pid>] [--not-before <date>] [--on-quota wait|stop]
#            [--reset-tz <tz>] -- <run-batch.sh options without the spec selectors>
#
#   queue file  one batch per line = run-batch selectors separated by spaces
#               ("115-117", "119 122 123", "126-129 130-adapter-dsh"). A line of exactly two
#               bare numbers ("118 124") is read as the range 118-124, not as two ids.
#   --wait-pid  start claiming only after that process (a batch already running) exits.
#   --not-before  start claiming only at/after that date (any `date -d` form).
#   --on-quota  what to do when run-batch exits 75 (provider quota): the unfinished
#               features go back to the FRONT of the queue either way; then `wait`
#               (default) sleeps until the reset time named in the error and continues,
#               `stop` ends the chain (CHAIN-STOPPED line).
#   A pool of N that refills as each feature ends = N chains with `--jobs 1` on a queue
#   of one feature per line; `--wait-pid` adds a worker when a run already in flight ends.
#   --reset-tz  timezone of the provider's "reset at" timestamp; default Asia/Shanghai
#               (z.ai prints Beijing time). Claude Code's "resets 9pm (Asia/Dubai)" names
#               its own timezone. Unparseable reset → wait 1 hour, then retry.
#
# Example — two harnesses draining one queue:
#   printf '%s\n' "112-117" "118-124" "125-129" > queue.txt
#   nohup chain.sh --queue queue.txt -- --harness claude --model fable   --reasoning high &
#   nohup chain.sh --queue queue.txt -- --harness dsh    --model glm-5.3 --reasoning max  &
set -uo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
Q=""; WAITPID=""; NOT_BEFORE=""; ON_QUOTA=wait; RESET_TZ=Asia/Shanghai
while [[ $# -gt 0 ]]; do
  case "$1" in
    --queue) Q="$2"; shift 2 ;;
    --wait-pid) WAITPID="$2"; shift 2 ;;
    --not-before) NOT_BEFORE="$2"; shift 2 ;;
    --on-quota) ON_QUOTA="$2"; shift 2 ;;
    --reset-tz) RESET_TZ="$2"; shift 2 ;;
    --) shift; break ;;
    *) echo "chain.sh: unknown option $1 (run-batch options go after --)" >&2; exit 2 ;;
  esac
done
[[ -n "$Q" && -f "$Q" ]] || { echo "chain.sh: --queue <file> is required" >&2; exit 2; }
[[ "$ON_QUOTA" == wait || "$ON_QUOTA" == stop ]] || { echo "chain.sh: --on-quota must be wait or stop" >&2; exit 2; }

# the harness name and log dir are needed only to find the status and requeue files
harness=""; logdir=""; args=("$@")
for ((i=0; i<${#args[@]}; i++)); do
  [[ "${args[i]}" == --harness ]] && harness="${args[i+1]}"
  [[ "${args[i]}" == --logdir ]] && logdir="${args[i+1]}"
done
LOGDIR="${logdir:-$(dirname "$Q")/logs}"
STATUS="$LOGDIR/${harness:-chain}.status"
# one requeue file per chain: several chains on one harness and logdir (a worker pool) would
# otherwise read and delete each other's unfinished features on a quota stop
REQUEUE="$LOGDIR/${harness:-chain}.requeue.$$"; export REQUEUE_FILE="$REQUEUE"
mkdir -p "$LOGDIR"

claim() {  # prints the claimed line, or nothing when the queue is empty
  exec 9>"$Q.lock"; flock 9
  local line; line="$(grep -m1 . "$Q")"
  [[ -n "$line" ]] && sed -i '0,/./{/./d}' "$Q"
  flock -u 9; printf '%s' "$line"
}
requeue_front() {  # $1 = a batch line; goes back to the top so it runs next
  exec 9>"$Q.lock"; flock 9
  { printf '%s\n' "$1"; cat "$Q"; } > "$Q.tmp" && mv "$Q.tmp" "$Q"
  flock -u 9
}
sleep_until() {  # $1 = epoch seconds
  local now; now="$(date +%s)"
  (( $1 > now )) && sleep $(( $1 - now ))
}

reset_epoch_of() {  # $@ = the features the quota cut off; prints the reset time (epoch) their logs name, or nothing
  local f line raw tz e now; now="$(date +%s)"
  for f in "$@"; do
    f="$LOGDIR/${harness:-chain}-$f.log"; [[ -f "$f" ]] || continue
    line="$(grep -oiE 'reset at [0-9: -]+' "$f" | tail -1)"
    if [[ -n "$line" ]]; then          # z.ai: "reset at 2026-10-01 03:13:42", in the provider's timezone
      e="$(TZ="$RESET_TZ" date -d "${line#* at }" +%s 2>/dev/null)" && { echo "$e"; return; }
    fi
    line="$(grep -oiE 'resets [^()]+\([^)]+\)' "$f" | tail -1)"
    if [[ -n "$line" ]]; then          # Claude Code: "resets 9pm (Asia/Dubai)", a clock time in the named timezone
      raw="${line#* }"; tz="${raw##*(}"; tz="${tz%)}"; raw="${raw%%(*}"; raw="${raw//,/}"
      e="$(TZ="$tz" date -d "$raw" +%s 2>/dev/null)" || continue
      # a bare clock time already passed: long ago = it means tomorrow; just now = the limit has reset
      if (( e < now )); then (( now - e > 43200 )) && e=$(( e + 86400 )) || e="$now"; fi
      echo "$e"; return
    fi
  done
}

if [[ -n "$WAITPID" ]]; then
  while kill -0 "$WAITPID" 2>/dev/null; do sleep 30; done
fi
if [[ -n "$NOT_BEFORE" ]]; then
  t="$(date -d "$NOT_BEFORE" +%s)" || { echo "chain.sh: cannot parse --not-before '$NOT_BEFORE'" >&2; exit 2; }
  echo "$(date -Is) CHAIN waiting until $(date -Is -d "@$t") before claiming" >> "$STATUS"
  sleep_until "$t"
fi

while batch="$(claim)"; [[ -n "$batch" ]]; do
  # legacy "first last" line -> range
  [[ "$batch" =~ ^[[:space:]]*([0-9]+)[[:space:]]+([0-9]+)[[:space:]]*$ ]] && batch="${BASH_REMATCH[1]}-${BASH_REMATCH[2]}"
  echo "$(date -Is) CHAIN claimed batch $batch" >> "$STATUS"
  rm -f "$REQUEUE"
  # shellcheck disable=SC2086  # the batch line is a list of selectors
  "$HERE/run-batch.sh" "$@" $batch
  rc=$?
  if (( rc == 75 )); then
    left="$(cat "$REQUEUE" 2>/dev/null)"
    [[ -n "$left" ]] && requeue_front "$left"
    reset_epoch="$(reset_epoch_of $left)"
    if [[ "$ON_QUOTA" == stop ]]; then
      echo "$(date -Is) CHAIN-STOPPED quota; re-queued: ${left:-nothing}${reset_epoch:+; provider resets $(date -Is -d "@$reset_epoch")}" >> "$STATUS"
      exit 75
    fi
    if [[ -n "$reset_epoch" ]]; then
      until_epoch=$(( reset_epoch + 60 ))
    else
      until_epoch=$(( $(date +%s) + 3600 ))
    fi
    echo "$(date -Is) CHAIN quota; re-queued: ${left:-nothing}; sleeping until $(date -Is -d "@$until_epoch")" >> "$STATUS"
    sleep_until "$until_epoch"
    sleep $(( RANDOM % 90 ))   # chains that stopped together must not all start a session in the same second
    echo "$(date -Is) CHAIN resuming after quota wait" >> "$STATUS"
  elif (( rc != 0 )); then
    # run-batch exits 0 even when features fail (their rc is in the status file); anything
    # else means the runner itself broke — stop instead of burning the rest of the queue
    requeue_front "$batch"
    echo "$(date -Is) CHAIN-STOPPED run-batch failed rc=$rc; re-queued: $batch" >> "$STATUS"
    exit "$rc"
  fi
done
rm -f "$REQUEUE"
echo "$(date -Is) CHAIN-DONE (queue empty)" >> "$STATUS"
