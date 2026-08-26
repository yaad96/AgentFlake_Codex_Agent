#!/usr/bin/env bash
# Run the 41 NIO containers sequentially, one pass@k sweep each.
# Resumable: a container that already has a completed run is skipped.
#
# PRUNE_ZIPS=1 deletes a subject archive only once NO REMAINING container in
# this list still needs it. That reference counting matters here: 41 NIO
# containers share just 14 archives, and quickcheckc1c alone is used by 26 of
# them -- pruning it eagerly would re-download 165 MB twenty-five times.
set -uo pipefail

# Associative arrays (declare -A) need bash >= 4. macOS ships bash 3.2, so
# this would silently mis-prune there; the target is the Ubuntu VM's bash 5.
if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: bash >= 4 required (found $BASH_VERSION). On macOS: brew install bash." >&2
  exit 1
fi

REPO="${REPO:-$HOME/AgentFlake_Codex_Agent}"
PY="$REPO/.venv/bin/python"
RUNNER="$REPO/AF_Codex_Agent/agentic/run_agentic_pass_at_k.py"
CSV="$REPO/AF_Codex_Agent/test_config.csv"
DATA="$REPO/AF_Codex_Agent/data"
RUNS="${RUNS:-1}"
MINGB="${MINGB:-8}"
LOGDIR="${LOGDIR:-$HOME/codex_nio_logs}"
mkdir -p "$LOGDIR"

CONTAINERS=(
elasticjobf0d
quickcheck9a0
quickcheckc1c2
elasticjob294
elasticjob003
hadoopbb6
elasticjob0031
quickcheck9a5
quickcheckd214
quickcheckc1c72
niohadoop15d02ea1
niohbaseb162d1a
niohbaseb162d1a2
niospringboot14ee4d4
niohadoop15d02ea2
quickcheckc1c1
quickcheckc1c3
quickcheckc1c4
quickcheckc1c6
quickcheckc1c5
quickcheckc1c11
quickcheckc1c12
quickcheckc1c13
quickcheckc1c14
quickcheckc1c15
quickcheckc1c46
quickcheckc1c47
quickcheckc1c48
quickcheckc1c49
quickcheckc1c50
quickcheckc1c51
quickcheckc1c52
quickcheckc1c53
quickcheckc1c54
quickcheckc1c55
quickcheckc1c56
quickcheckc1c57
quickcheckc1c58
quickcheckc1c59
quickcheck4860
quickcheck4861
)

[[ -x "$PY" ]]     || { echo "ERROR: no venv interpreter at $PY (run setup.sh)"; exit 1; }
[[ -f "$RUNNER" ]] || { echo "ERROR: runner not found at $RUNNER"; exit 1; }
[[ -f "$CSV" ]]    || { echo "ERROR: test_config.csv not found at $CSV"; exit 1; }
KEYFILE="$REPO/AF_Codex_Agent/.openai_api_key"
[[ -s "$KEYFILE" ]] || { echo "ERROR: no key in $KEYFILE"; exit 1; }
KEY="$(head -1 "$KEYFILE" | tr -d '[:space:]')"
code=$(curl -sS -o /tmp/preflight.json -w '%{http_code}' \
  -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
  -d '{"model":"gpt-5.4","input":"hi","max_output_tokens":16}' \
  https://api.openai.com/v1/responses)
if [[ "$code" != "200" ]]; then
  echo "ERROR: API pre-flight failed (http=$code). Not starting the batch."
  cat /tmp/preflight.json; exit 1
fi
mkdir -p "$DATA"
avail=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc '0-9')
if [[ -n "$avail" && "$avail" -lt "$MINGB" ]]; then
  echo "ERROR: only ${avail}G free under $DATA (need >= ${MINGB}G). Override with MINGB=<n>."
  exit 1
fi

# zip name per container, and the LAST index at which each zip is still needed
declare -A ZIPOF LASTUSE
for idx in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$idx]}"
  z=$(awk -F',' -v c="$c" 'NR>1 && $2==c {print $3; exit}' "$CSV")
  ZIPOF["$c"]="$z"
  [[ -n "$z" ]] && LASTUSE["$z"]=$idx
done

echo "[batch] pre-flight OK (API 200, ${avail:-?}G free, prune_zips=${PRUNE_ZIPS:-0})"
echo "[batch] ${#CONTAINERS[@]} containers, ${#LASTUSE[@]} distinct archives, runs=$RUNS"

started=$(date +%s)
declare -i skip=0 fail=0 n=0
for idx in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$idx]}"; n+=1
  if compgen -G "$DATA/$c/run_*/.run_complete" >/dev/null 2>&1; then
    echo "[$n/${#CONTAINERS[@]}] SKIP  $c"; skip+=1
  else
    free_now=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc '0-9')
    echo "[$n/${#CONTAINERS[@]}] RUN   $c   (${free_now:-?}G free)"
    t0=$(date +%s)
    if "$PY" "$RUNNER" "$c" --runs "$RUNS" > "$LOGDIR/$c.log" 2>&1; then st="ok"; else st="FAILED"; fail+=1; fi
    echo "      -> $st  $(( $(date +%s)-t0 ))s  $(grep -oE 'DONE\. [0-9]+/[0-9]+ runs PASSED[^ ]*' "$LOGDIR/$c.log" | tail -1)"
  fi
  # prune only when this is the final container needing that archive
  if [[ "${PRUNE_ZIPS:-0}" == "1" ]]; then
    z="${ZIPOF[$c]:-}"
    if [[ -n "$z" && "${LASTUSE[$z]}" == "$idx" && -f "$DATA/$z.zip" ]]; then
      rm -f "$DATA/$z.zip"; echo "      pruned $z.zip (no remaining container needs it)"
    fi
  fi
done
echo
echo "[batch] finished in $(( ($(date +%s)-started)/60 )) min  attempted=$n skipped=$skip failures=$fail"
