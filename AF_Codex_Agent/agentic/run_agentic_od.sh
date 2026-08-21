#!/usr/bin/env bash
# ============================================================
# run_agentic_od.sh — agentic OD repair pipeline
#
# Mirrors run_od_tracemop.sh's setup (steps 1-7) but replaces steps
# 8-11 with a single call to the
# Codex CLI driver, which runs an iterative tool-use loop with bounded
# attempts.
#
# Steps performed (output dir = data/<container>/run_<NN>/codex_outputs/):
#   1.  unzip + apply Fixed.patch
#   2.  start docker container with parent data dir mounted
#   3.  run mvn surefire:test on Flaky/ -> traces-flaky/mvn.log
#   9.5 snapshot Flaky/ -> Flaky.pristine + write trace_config.json
#   AGENT  agentic_codex_cli.py        -> llm_response.json
#                                            apply_report.json
#                                            verify_after_fix.{log,verdict}
#                                            agentic_conversation.json
#                                            agentic_iterations.jsonl
#
# Usage:
#   ./run_agentic_od.sh <result_container> [options]
#
# Requires:
#   OPENAI_API_KEY in the environment or .openai_api_key + install AF_Codex_Agent/requirements.txt
# ============================================================

set -euo pipefail

# ---- CLI options (positional container + optional flags) -------------------
RESULT_CONTAINER=""
FORCE_REBUILD_IMAGE=0
VERIFY_PASS_RUNS=""
CLI_TIMEOUT_S=""

usage() {
  cat >&2 <<USAGE
Usage: $0 <result_container> [options]

Options:
  --force-rebuild-image     rebuild the Docker image even if one already exists
  --verify-pass-runs <n>    extra passing verification runs after the first pass
  --cli-timeout-s <sec>     wall-clock cap for Codex
  -h, --help                show this help
USAGE
}

while (( $# )); do
  case "$1" in
    --force-rebuild-image) FORCE_REBUILD_IMAGE=1; shift ;;
    --verify-pass-runs) VERIFY_PASS_RUNS="${2:?--verify-pass-runs needs a value}"; shift 2 ;;
    --cli-timeout-s)    CLI_TIMEOUT_S="${2:?--cli-timeout-s needs a value}";     shift 2 ;;
    -h|--help) usage; exit 0 ;;
    --*) echo "ERROR: unknown option '$1'" >&2; usage; exit 2 ;;
    *)
      if [[ -n "$RESULT_CONTAINER" ]]; then
        echo "ERROR: unexpected argument '$1'" >&2; usage; exit 2
      fi
      RESULT_CONTAINER="$1"; shift ;;
  esac
done

if [[ -z "$RESULT_CONTAINER" ]]; then
  echo "ERROR: <result_container> is required" >&2; usage; exit 2
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPROFLAKE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
OPENAI_API_KEY_FILE="$REPROFLAKE_DIR/.openai_api_key"

if [[ -z "${OPENAI_API_KEY:-}" && -f "$OPENAI_API_KEY_FILE" ]]; then
  OPENAI_API_KEY="$(sed -n "s/^[[:space:]]*//; s/[[:space:]]*$//; /^[#]/d; /^$/d; p; q" "$OPENAI_API_KEY_FILE")"
  export OPENAI_API_KEY
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: OPENAI_API_KEY is required. Export it or put it in $OPENAI_API_KEY_FILE."; exit 1
fi

DATA_ROOT="$REPROFLAKE_DIR/data/$RESULT_CONTAINER"
if [[ -n "${AGENTIC_RUN_LABEL:-}" ]]; then
  RUN_LABEL="$AGENTIC_RUN_LABEL"
else
  n=1
  while :; do
    RUN_LABEL="$(printf 'run_%02d' "$n")"
    [[ ! -e "$DATA_ROOT/$RUN_LABEL" ]] && break
    n=$((n + 1))
  done
fi
if [[ ! "$RUN_LABEL" =~ ^run_[0-9]+$ ]]; then
  echo "ERROR: AGENTIC_RUN_LABEL must look like run_NN (got '$RUN_LABEL')."; exit 1
fi
export AGENTIC_RUN_LABEL="$RUN_LABEL"
DATA_DIR="$DATA_ROOT/$RUN_LABEL"
CODEX_INPUTS_DIR="$DATA_DIR/codex_inputs"
CODEX_OUTPUTS_DIR="$DATA_DIR/codex_outputs"
STEPS_OUT_DIR="$CODEX_OUTPUTS_DIR"
CSV="$REPROFLAKE_DIR/test_config.csv"

# ----- parse CSV row ----------------------------------------
[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "od" ]]; then
  echo "ERROR: this script targets od only; got '$TEST_TYPE'."
  exit 1
fi
if [[ -z "$POLLUTER" || -z "$VICTIM" ]]; then
  echo "ERROR: OD container '$RESULT_CONTAINER' must have both polluter and victim in CSV."
  exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8_od_cov";  DOCKERFILE="Dockerfile.od" ;;
  11) IMAGE="flaky_base_jdk11_od_cov"; DOCKERFILE="Dockerfile.od11" ;;
  *)  echo "ERROR: OD with java=$JAVA not supported"; exit 1 ;;
esac
PROJECT_KEY="$(printf '%s\n' "$MODULE" | tr '[:upper:]' '[:lower:]')"
if [[ "$PROJECT_KEY" == *hadoop* ]]; then
  IMAGE="flaky_base_jdk8_hadoop"
  DOCKERFILE="Dockerfile.hadoop"
fi

DOCKER_PLATFORM_ARGS=()
if [[ -n "${AGENTIC_DOCKER_PLATFORM:-}" ]]; then
  DOCKER_PLATFORM_ARGS=(--platform "$AGENTIC_DOCKER_PLATFORM")
elif [[ "$(uname -s)" == "Darwin" && "$(uname -m)" == "arm64" ]]; then
  DOCKER_PLATFORM_ARGS=(--platform linux/amd64)
fi
if ((${#DOCKER_PLATFORM_ARGS[@]})); then
  echo "[setup] Docker platform: ${DOCKER_PLATFORM_ARGS[*]}"
fi

image_has_codex() {
  docker run --rm --entrypoint sh "$1" -lc 'command -v codex >/dev/null 2>&1'
}

ensure_docker_image() {
  local image="$1"
  local dockerfile="${2:-}"

  if [[ "$FORCE_REBUILD_IMAGE" == "1" ]] && docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] force rebuilding Docker image '$image'"
  elif ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] Docker image '$image' not found"
  elif image_has_codex "$image"; then
    echo "[setup] Docker image '$image' is ready"
    return 0
  else
    echo "[setup] Docker image '$image' exists but lacks Codex CLI or cannot run"
  fi

  if [[ -z "$dockerfile" ]]; then
    echo "ERROR: image '$image' is missing/stale and no Dockerfile is available in this repo." >&2
    echo "       Rebuild or install an image with the Codex CLI, or choose a supported Java/test-type combination." >&2
    exit 1
  fi
  echo "[setup] building Docker image '$image' from $dockerfile"
  docker build "${DOCKER_PLATFORM_ARGS[@]}" -t "$image" -f "$REPROFLAKE_DIR/$dockerfile" "$REPROFLAKE_DIR"
}

ensure_docker_image "$IMAGE" "${DOCKERFILE:-}"

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"

cleanup_container() {
  local rc=$?
  [[ "${KEEP_CONTAINER:-0}" == "1" ]] && return $rc
  if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "$CONTAINER"; then
    echo "[cleanup] removing container '$CONTAINER' (set KEEP_CONTAINER=1 to skip)"
    docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  fi
  return $rc
}
trap cleanup_container EXIT

cat <<EOF
==========================================
[AGENTIC OD]
result_container : $RESULT_CONTAINER
test_type        : $TEST_TYPE
module           : $MODULE
polluter         : $POLLUTER
victim           : $VICTIM
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
data dir         : $DATA_DIR
==========================================
EOF

# ============================================================
# STEP 0 — START-OF-RUN CLEANUP (mirrors run_od_tracemop.sh)
# ============================================================
if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
  echo "[step 0 ] Cleaning mutated source dirs from previous run"
  rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/FlakyCodeChange" "$DATA_DIR/Flaky" \
         "$DATA_DIR/Flakym2" "$DATA_DIR/Flaky.pristine" "$DATA_DIR/result"
fi

# ============================================================
# STEP 1 — unzip + apply Fixed.patch
# ============================================================
need_step1=0
for d in Fixed Flaky Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || need_step1=1
done

if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and CSV URL is empty"; exit 1; }
    echo "[step 1a] Downloading $URL -> $ZIP_PATH"
    mkdir -p "$REPROFLAKE_DIR/data"
    if   command -v curl >/dev/null; then curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null; then wget "$URL" -O "$ZIP_PATH"
    else echo "ERROR: need curl or wget"; exit 1
    fi
  fi
  if [[ ! -d "$DATA_DIR/Flaky" || ! -d "$DATA_DIR/Flakym2" ]]; then
    echo "[step 1a] Unzipping $ZIP_PATH"
    mkdir -p "$DATA_DIR"
    unzip -o "$ZIP_PATH" -d "$DATA_DIR" > /dev/null
    if [[ -d "$DATA_DIR/$ZIP" ]]; then
      mv "$DATA_DIR/$ZIP/"* "$DATA_DIR/" 2>/dev/null || true
      rmdir "$DATA_DIR/$ZIP" 2>/dev/null || true
    fi
  fi
  if [[ ! -d "$DATA_DIR/Fixed" ]]; then
    [[ -f "$DATA_DIR/Fixed.patch" ]] || { echo "ERROR: $DATA_DIR/Fixed.patch missing"; exit 1; }
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Fixed"
    patch -p1 -d "$DATA_DIR/Fixed" < "$DATA_DIR/Fixed.patch" >/dev/null
  fi
else
  echo "[step 1c] Fixed/, Flaky/, Flakym2/ already present — skipping unzip."
fi

# ============================================================
# STEP 2 — Start container
# ============================================================
echo "[step 2 ] Starting container '$CONTAINER' from image '$IMAGE'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# ============================================================
# STEP 3 — Run Flaky to capture initial failure log.
# ============================================================
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

echo "[step 3 ] /app/work/Flaky -> /app/work/traces-flaky (failure log)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flaky; mkdir -p /app/work/traces-flaky
  cd /app/work/Flaky
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn surefire:test \
    -pl $MODULE -Dtest='$POLLUTER,$VICTIM' \
    -Dsurefire.runOrder=testorder \
    $MVNOPTS 2>&1 | tee /app/work/traces-flaky/mvn.log || true
"

# Sanity: the Flaky run must actually fail before we hand off to the agent.
echo "[sanity ] Verifying the Flaky run produced a test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-flaky/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: no Surefire summary line in traces-flaky/mvn.log"
  exit 1
fi
TESTS=$(  sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$SUMMARY"); TESTS=${TESTS:-0}
FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$SUMMARY"); FAILURES=${FAILURES:-0}
ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY"); ERRORS=${ERRORS:-0}
if (( TESTS < 1 || FAILURES + ERRORS < 1 )); then
  echo "ERROR: Flaky run did not fail as expected (Tests=$TESTS Failures=$FAILURES Errors=$ERRORS)"
  exit 1
fi
echo "[sanity ] Flaky run failed as expected (Tests=$TESTS Failures=$FAILURES Errors=$ERRORS)"

mkdir -p "$CODEX_INPUTS_DIR" "$CODEX_OUTPUTS_DIR"

# ============================================================
# STEP 9.5 — snapshot Flaky/ for between-iteration restore
# ============================================================
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$CODEX_INPUTS_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "od",
  "module": "$MODULE",
  "polluter": "$POLLUTER",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": false
}
JSONEOF

# ============================================================
# AGENT — bounded-iteration tool-use loop
# ============================================================
  echo "[agent ] launching agentic_codex_cli.py (Codex agent, model=${AGENTIC_MODEL:-gpt-5.4})"
  set +e
  "${AGENTIC_PYTHON:-python3}" "$SCRIPT_DIR/agentic_codex_cli.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --model "${AGENTIC_MODEL:-gpt-5.4}" \
    ${VERIFY_PASS_RUNS:+--verify-pass-runs "$VERIFY_PASS_RUNS"} \
    ${CLI_TIMEOUT_S:+--cli-timeout-s "$CLI_TIMEOUT_S"}
  AGENT_RC=$?
  set -e

# Clean up the snapshot.
cleanup_completed_source_dirs() {
  local verdict=""
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/run_verdict.txt")"
  elif [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi

  if [[ "$verdict" == "PASSED" || "$verdict" == "FAILED" ]]; then
    echo "[cleanup] removing completed-run source dirs: Fixed Flaky Flakym2 FlakyCodeChange"
    if command -v docker >/dev/null 2>&1; then
      docker exec -u 0 "$CONTAINER" chown -R "$(id -u):$(id -g)" /app/work >/dev/null 2>&1 || true
    fi
    rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/Flaky" "$DATA_DIR/Flakym2" "$DATA_DIR/FlakyCodeChange" ||         echo "[cleanup] WARNING: failed to remove one or more source dirs" >&2
  fi
}
cleanup_completed_source_dirs

rm -rf "$DATA_DIR/Flaky.pristine"

echo
echo "=========================================="
echo "[AGENTIC OD] Done."
echo "Trace dirs (mvn.log):"
for v in flaky fixed; do
  d="$DATA_DIR/traces-$v"
  if [[ -f "$d/mvn.log" ]]; then
    sz=$(wc -c < "$d/mvn.log" | tr -d ' ')
    printf "  traces-%-8s  mvn.log=%s bytes\n" "$v" "$sz"
  fi
done
echo
echo "Codex outputs ($CODEX_OUTPUTS_DIR/):"
for f in run_summary.csv trace_config.json rv_trace_diff.log llm_trace_summary.txt llm_context.txt \
         llm_response.json apply_report.json verify_after_fix.log \
         verify_after_fix.verdict agentic_conversation.json \
         agentic_iterations.jsonl; do
  if [[ -f "$STEPS_OUT_DIR/$f" ]]; then
    sz=$(wc -c < "$STEPS_OUT_DIR/$f" | tr -d ' ')
    printf "  %-30s  %s bytes\n" "$f" "$sz"
  fi
done

if [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
  echo
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/run_verdict.txt")   (verification: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null))"
  else
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi
fi

echo "=========================================="
exit $AGENT_RC
