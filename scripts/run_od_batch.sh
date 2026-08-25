#!/usr/bin/env bash
# Run the 41 OD containers sequentially, one pass@k sweep each.
# Resumable: a container whose run_NN/.run_complete already exists is skipped.
# Never aborts the batch on a single failure -- it records and moves on.
set -uo pipefail

REPO="${REPO:-$HOME/AgentFlake_Codex_Agent}"
PY="$REPO/.venv/bin/python"
RUNNER="$REPO/AF_Codex_Agent/agentic/run_agentic_pass_at_k.py"
DATA="$REPO/AF_Codex_Agent/data"
RUNS="${RUNS:-1}"
LOGDIR="${LOGDIR:-$HOME/codex_od_logs}"

mkdir -p "$LOGDIR"

CONTAINERS=(
shardingsphereelasticjobelasticjoblitecore23a2ab6
jnrposixd9f3f84
dubbodubborpcdubborpcapiba89f441
shardingsphereelasticjobelasticjoblitecore4b9afa4
wikidatatoolkitwdtkutil10f9711
ACCUMULO-2102_testSetInstance_HdfsZooInstance_HostsGiven
dubbodubborpcdubborpcdubboaa9f16e
wildflynaming3a83b7b1
marineapi0a1f309
ormlitecore59309e5
oddubbo1
oduniversalgcodesender1
oduniversalgcodesender2
oduniversalgcodesender3
odshardingsphereelasticjob1
marineapi0a1f308
ACCUMULO-2102_testSetInstance_HdfsZooInstance_InstanceGiven
ormlitecore59309e6
wildflynaming3a83b7b21
dubbodubborpcdubborpcapiba89f44
shardingsphereelasticjobelasticjoblitecore23a2ab5
ormlitecore59309e10
wildflynaming3a83b7b20
dubbodubborpcdubborpcdubbo628ad771
ACCUMULO-2102_testSetInstance_HdfsZooInstance_Explicit
shardingsphereelasticjobelasticjoblitecore90e3a7f
ACCUMULO-2102_testSetInstance_HdfsZooInstance_Implicit
dubbodubborpcdubborpcdubbo628ad77
wildflynaming3a83b7b19
wildflynaming3a83b7b18
wildflynaming3a83b7b17
wikidatatoolkitwdtkutil10f9712
wildflynaming3a83b7b13
wildflynaming3a83b7b12
wildflynaming3a83b7b11
wildflynaming3a83b7b10
ormlitecore59309e90
ormlitecore59309e89
ormlitecore59309e88
ormlitecore59309e59
ormlitecore59309e60
)

# --- pre-flight: fail fast rather than 41 times over ------------------------
[[ -x "$PY" ]]      || { echo "ERROR: no venv interpreter at $PY (run setup.sh)"; exit 1; }
[[ -f "$RUNNER" ]]  || { echo "ERROR: runner not found at $RUNNER"; exit 1; }
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
echo "[batch] API pre-flight OK; ${#CONTAINERS[@]} containers, runs=$RUNS"
echo "[batch] logs -> $LOGDIR"

started=$(date +%s)
declare -i done_n=0 skip_n=0 fail_n=0

for c in "${CONTAINERS[@]}"; do
  done_n+=1
  if compgen -G "$DATA/$c/run_*/.run_complete" >/dev/null 2>&1; then
    echo "[$done_n/${#CONTAINERS[@]}] SKIP  $c (already has a completed run)"
    skip_n+=1
    continue
  fi
  echo "[$done_n/${#CONTAINERS[@]}] RUN   $c"
  t0=$(date +%s)
  if "$PY" "$RUNNER" "$c" --runs "$RUNS" > "$LOGDIR/$c.log" 2>&1; then
    status="ok"
  else
    status="FAILED(exit=$?)"
    fail_n+=1
  fi
  t1=$(date +%s)
  verdict=$(grep -oE 'DONE\. [0-9]+/[0-9]+ runs PASSED[^ ]*' "$LOGDIR/$c.log" | tail -1)
  echo "      -> $status  $((t1-t0))s  ${verdict:-no summary line}"
done

echo
echo "[batch] finished in $(( ($(date +%s)-started)/60 )) min"
echo "[batch] attempted=$done_n skipped=$skip_n wrapper_failures=$fail_n"
echo "[batch] summary CSV: $REPO/AF_Codex_Agent/Complete_Containers_Summary.csv"
