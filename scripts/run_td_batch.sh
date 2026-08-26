#!/usr/bin/env bash
# TD batch. TWO MODES:
#
#   DRYRUN=1  zero-API reproduction sweep. AGENTIC_SIMULATED_AGENT replaces the
#             model call, but download / build / FlakyCodeChange forcing / the
#             reproduction sanity gate all run for real. Use this FIRST to learn
#             which containers actually reproduce on this machine -- costs nothing.
#
#   (default) real runs, calling the model.
#
# The TD reproduction gate in run_agentic_td.sh exits at line ~311, well BEFORE
# the agent launches (~line 353), so a container whose forcing does not fail
# never reaches the API. Non-reproducing containers are free either way; the
# dry run just lets you find them without also paying for the ones that do.
set -uo pipefail
if (( BASH_VERSINFO[0] < 4 )); then
  echo "ERROR: bash >= 4 required (found $BASH_VERSION)." >&2; exit 1
fi

REPO="${REPO:-$HOME/AgentFlake_Codex_Agent}"
PY="$REPO/.venv/bin/python"
RUNNER="$REPO/AF_Codex_Agent/agentic/run_agentic_pass_at_k.py"
CSV="$REPO/AF_Codex_Agent/test_config.csv"
DATA="$REPO/AF_Codex_Agent/data"
RUNS="${RUNS:-1}"
MINGB="${MINGB:-8}"
DRYRUN="${DRYRUN:-0}"
LOGDIR="${LOGDIR:-$HOME/codex_td_logs}"
[[ "$DRYRUN" == "1" ]] && LOGDIR="${LOGDIR}_dry"
mkdir -p "$LOGDIR"

CONTAINERS=(
BOOKKEEPER-846
COLLECTIONS-812
BOOKKEEPER-709
HBASE-27051
ZOOKEEPER-4327-testGlobalOutstandingRequestThrottlingWithRequestThrottlerDisabled
RATIS-1363
APEXCORE-617-testEmitTuplesOutsideStreamingWindow
OOZIE-3683
CAMEL-12025-testToFunction
YARN-9551
tdwro4jcore1
tdjavawebsocket1
logback1
dbschedular1
tdincubatoruniffle1
CAMEL-12025-testFromStreamTimer
CAMEL-12025-testMultipleSubscriptionsWithTimer
CAMEL-12025-testFrom
CAMEL-12025-testToFunctionWithExchange
CAMEL-17188
HDFS-10281
OOZIE-3685
OOZIE-3686
CURATOR-681
CAMEL-12025-testTo
APEXCORE-403
CAMEL-12025-testToWithExchange
CAMEL-15580
CURATOR-671
HADOOP-10394_testDoFilterAuthentication
HADOOP-10394_testDoFilterAuthenticationWithDomainPath
HADOOP-10394_testDoFilterAuthenticationWithInvalidToken
HADOOP-12181
HADOOP-12588
HDFS-11682
HDFS-15702
YARN-1268
YARN-9405
YARN-9768
ZOOKEEPER-4327-testRequestThrottler
)

[[ -x "$PY" ]]     || { echo "ERROR: no venv interpreter at $PY"; exit 1; }
[[ -f "$RUNNER" ]] || { echo "ERROR: runner not found at $RUNNER"; exit 1; }
[[ -f "$CSV" ]]    || { echo "ERROR: test_config.csv not found at $CSV"; exit 1; }

if [[ "$DRYRUN" == "1" ]]; then
  export AGENTIC_SIMULATED_AGENT="${AGENTIC_SIMULATED_AGENT:-$LOGDIR/.noop_fixture}"
  mkdir -p "$AGENTIC_SIMULATED_AGENT"      # no patch.diff -> models a no-op agent
  echo "[batch] DRY RUN: no API calls. Verdicts will be FAILED by construction;"
  echo "[batch]          what matters is whether the TD forcing REPRODUCED."
else
  KEYFILE="$REPO/AF_Codex_Agent/.openai_api_key"
  [[ -s "$KEYFILE" ]] || { echo "ERROR: no key in $KEYFILE"; exit 1; }
  KEY="$(head -1 "$KEYFILE" | tr -d '[:space:]')"
  code=$(curl -sS -o /tmp/preflight.json -w '%{http_code}' \
    -H "Authorization: Bearer $KEY" -H "Content-Type: application/json" \
    -d '{"model":"gpt-5.4","input":"hi","max_output_tokens":16}' \
    https://api.openai.com/v1/responses)
  [[ "$code" == "200" ]] || { echo "ERROR: API pre-flight failed (http=$code)"; cat /tmp/preflight.json; exit 1; }
  echo "[batch] API pre-flight OK"
fi

mkdir -p "$DATA"
avail=$(df -BG --output=avail "$DATA" 2>/dev/null | tail -1 | tr -dc '0-9')
[[ -n "$avail" && "$avail" -lt "$MINGB" ]] && { echo "ERROR: only ${avail}G free (need >= ${MINGB}G)"; exit 1; }

declare -A ZIPOF LASTUSE
for idx in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$idx]}"
  z=$(awk -F',' -v c="$c" 'NR>1 && $2==c {print $3; exit}' "$CSV")
  ZIPOF["$c"]="$z"; [[ -n "$z" ]] && LASTUSE["$z"]=$idx
done
echo "[batch] ${#CONTAINERS[@]} containers, ${#LASTUSE[@]} archives, dryrun=$DRYRUN, ${avail:-?}G free"

REPRO_OK="$LOGDIR/reproduced.txt"; : > "$REPRO_OK"
NO_REPRO="$LOGDIR/not_reproduced.txt"; : > "$NO_REPRO"
started=$(date +%s); declare -i n=0 rep=0 norep=0
for idx in "${!CONTAINERS[@]}"; do
  c="${CONTAINERS[$idx]}"; n+=1
  echo "[$n/${#CONTAINERS[@]}] $c"
  t0=$(date +%s)
  "$PY" "$RUNNER" "$c" --runs "$RUNS" > "$LOGDIR/$c.log" 2>&1
  # the sanity gate is the signal we care about, in either mode
  if grep -q 'TD flakiness not reproduced\|TD failure not reproduced\|cannot reproduce the TD failure' "$LOGDIR/$c.log"; then
    echo "      -> NOT REPRODUCED (no API spent)  $(( $(date +%s)-t0 ))s"
    echo "$c" >> "$NO_REPRO"; norep+=1
  elif grep -q 'FlakyCodeChange: Tests=' "$LOGDIR/$c.log"; then
    echo "      -> reproduced  $(( $(date +%s)-t0 ))s  $(grep -oE 'DONE\. [0-9]+/[0-9]+ runs PASSED[^ ]*' "$LOGDIR/$c.log" | tail -1)"
    echo "$c" >> "$REPRO_OK"; rep+=1
  else
    echo "      -> setup failed before the gate  $(( $(date +%s)-t0 ))s  (see $LOGDIR/$c.log)"
    echo "$c" >> "$NO_REPRO"; norep+=1
  fi
  if [[ "${PRUNE_ZIPS:-0}" == "1" ]]; then
    z="${ZIPOF[$c]:-}"
    [[ -n "$z" && "${LASTUSE[$z]}" == "$idx" && -f "$DATA/$z.zip" ]] && { rm -f "$DATA/$z.zip"; echo "      pruned $z.zip"; }
  fi
done
echo
echo "[batch] done in $(( ($(date +%s)-started)/60 )) min"
echo "[batch] reproduced: $rep    not reproduced: $norep"
echo "[batch] viable list -> $REPRO_OK"
echo "[batch] excluded    -> $NO_REPRO"
