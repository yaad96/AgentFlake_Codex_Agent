#!/usr/bin/env bash
# ============================================================
# run_agentic_td.sh — agentic TD repair pipeline
#
# Mirrors run_td_tracemop.sh's setup (steps 1-7) but replaces steps
# 8-11 with a call to agentic_claude_cli.py. The Claude CLI agent then iterates
# through context tools up to the configured Claude Code turn cap.
#
# Usage:  ./run_agentic_td.sh <result_container> [options]
# Requires: ANTHROPIC_API_KEY or .anthropic_api_key + install AF_Codex_Agent/requirements.txt
# ============================================================

set -euo pipefail

# ---- CLI options (positional container + optional flags) -------------------
RESULT_CONTAINER=""
FORCE_REBUILD_IMAGE=0
MAX_BUDGET_USD=""
VERIFY_PASS_RUNS=""
CLI_TIMEOUT_S=""

usage() {
  cat >&2 <<USAGE
Usage: $0 <result_container> [options]

Options:
  --force-rebuild-image     rebuild the Docker image even if one already exists
  --max-budget-usd <usd>    hard Claude Code spend cap for this run
  --verify-pass-runs <n>    extra passing verification runs after the first pass
  --cli-timeout-s <sec>     wall-clock cap for Claude Code
  -h, --help                show this help
USAGE
}

while (( $# )); do
  case "$1" in
    --force-rebuild-image) FORCE_REBUILD_IMAGE=1; shift ;;
    --max-budget-usd)   MAX_BUDGET_USD="${2:?--max-budget-usd needs a value}";   shift 2 ;;
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
ANTHROPIC_API_KEY_FILE="$REPROFLAKE_DIR/.anthropic_api_key"

if [[ -z "${ANTHROPIC_API_KEY:-}" && -f "$ANTHROPIC_API_KEY_FILE" ]]; then
  ANTHROPIC_API_KEY="$(sed -n "s/^[[:space:]]*//; s/[[:space:]]*$//; /^[#]/d; /^$/d; p; q" "$ANTHROPIC_API_KEY_FILE")"
  export ANTHROPIC_API_KEY
fi

# AGENTIC_SIMULATED_AGENT replays a recorded agent instead of calling the API,
# so no key is needed; everything after the agent still runs for real.
if [[ -z "${ANTHROPIC_API_KEY:-}" && -z "${AGENTIC_SIMULATED_AGENT:-}" ]]; then
  echo "ERROR: ANTHROPIC_API_KEY is required. Export it or put it in $ANTHROPIC_API_KEY_FILE."; exit 1
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
CLAUDE_INPUTS_DIR="$DATA_DIR/claude_inputs"
CLAUDE_OUTPUTS_DIR="$DATA_DIR/claude_outputs"
STEPS_OUT_DIR="$CLAUDE_OUTPUTS_DIR"
CSV="$REPROFLAKE_DIR/test_config.csv"

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "td" ]]; then
  echo "ERROR: this script targets td only; got '$TEST_TYPE'."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk8"; DOCKERFILE="Dockerfile" ;;
  11) IMAGE="flaky_base_jdk11" ;;
  17) IMAGE="flaky_base_jdk17" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
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

image_has_claude() {
  docker run --rm --entrypoint sh "$1" -lc 'command -v claude >/dev/null 2>&1'
}

ensure_docker_image() {
  local image="$1"
  local dockerfile="${2:-}"

  if [[ "$FORCE_REBUILD_IMAGE" == "1" ]] && docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] force rebuilding Docker image '$image'"
  elif ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "[setup] Docker image '$image' not found"
  elif image_has_claude "$image"; then
    echo "[setup] Docker image '$image' is ready"
    return 0
  else
    echo "[setup] Docker image '$image' exists but lacks Claude CLI or cannot run"
  fi

  if [[ -z "$dockerfile" ]]; then
    echo "ERROR: image '$image' is missing/stale and no Dockerfile is available in this repo." >&2
    echo "       Rebuild or install an image with the Claude CLI, or choose a supported Java/test-type combination." >&2
    exit 1
  fi
  echo "[setup] building Docker image '$image' from $dockerfile"
  docker build "${DOCKER_PLATFORM_ARGS[@]}" -t "$image" -f "$REPROFLAKE_DIR/$dockerfile" "$REPROFLAKE_DIR"
}

ensure_docker_image "$IMAGE" "${DOCKERFILE:-}"

CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"
cleanup_container() {
  local rc=$?
  local completed_failed=0
  if (( rc != 0 )) \
      && [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]] \
      && [[ "$(tr -d '[:space:]' < "$STEPS_OUT_DIR/run_verdict.txt")" == "FAILED" ]] \
      && [[ -f "$STEPS_OUT_DIR/td_validation/aggregate.json" ]] \
      && grep -Eq '"terminal_ready"[[:space:]]*:[[:space:]]*true' \
           "$STEPS_OUT_DIR/td_validation/aggregate.json" \
      && grep -Eq '"verdict"[[:space:]]*:[[:space:]]*"FAILED"' \
           "$STEPS_OUT_DIR/td_validation/aggregate.json"; then
    completed_failed=1
  fi
  if (( rc != 0 && completed_failed == 0 )); then
    mkdir -p "$STEPS_OUT_DIR/td_validation"
    printf 'FAILED\n' > "$STEPS_OUT_DIR/run_verdict.txt"
    printf 'FAILED\n' > "$STEPS_OUT_DIR/verify_after_fix.verdict"
    {
      printf '{\n'
      printf '  "schema_version": 1,\n'
      printf '  "terminal_ready": true,\n'
      printf '  "verdict": "FAILED",\n'
      printf '  "internal_status": "INCOMPLETE",\n'
      printf '  "evaluation_incomplete": true,\n'
      printf '  "reason_code": "LAUNCHER_EXIT_%s_FAIL_CLOSED",\n' "$rc"
      printf '  "requested_attempts": 0,\n'
      printf '  "actual_attempts": 0,\n'
      printf '  "valid_attempts": 0,\n'
      printf '  "passed_attempts": 0,\n'
      printf '  "failed_attempts": 0,\n'
      printf '  "incomplete_attempts": 0,\n'
      printf '  "runs": []\n'
      printf '}\n'
    } > "$STEPS_OUT_DIR/td_validation/aggregate.json"
  fi
  [[ "${KEEP_CONTAINER:-0}" == "1" ]] && return $rc
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  return $rc
}
trap cleanup_container EXIT

cat <<EOF
==========================================
[AGENTIC TD]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
==========================================
EOF

# STEP 0 — cleanup
if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/FixedCodeChange" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/FlakyCodeChange" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
  echo "[step 0 ] Cleaning mutated source dirs from previous run"
  rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/FixedCodeChange" \
         "$DATA_DIR/FlakyCodeChange" "$DATA_DIR/Flaky" \
         "$DATA_DIR/Flakym2" "$DATA_DIR/Flaky.pristine" "$DATA_DIR/result"
fi

# STEP 1 — unzip + patches
need_step1=0
for d in Fixed FixedCodeChange FlakyCodeChange Flakym2; do
  [[ -d "$DATA_DIR/$d" ]] || need_step1=1
done
if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and URL empty"; exit 1; }
    echo "[step 1a] Downloading $URL"
    mkdir -p "$REPROFLAKE_DIR/data"
    if   command -v curl >/dev/null; then curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null; then wget "$URL" -O "$ZIP_PATH"
    else echo "ERROR: need curl or wget"; exit 1; fi
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
  apply_variant() {
    local target="$1" patch_file="$2"
    [[ -d "$DATA_DIR/$target" ]] && return
    [[ -f "$DATA_DIR/$patch_file" ]] || { echo "ERROR: $DATA_DIR/$patch_file missing"; exit 1; }
    echo "[step 1b] Creating $target/ = Flaky/ + $patch_file"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/$target"
    patch -p1 -d "$DATA_DIR/$target" < "$DATA_DIR/$patch_file" >/dev/null
  }
  apply_variant "Fixed"           "Fixed.patch"
  apply_variant "FixedCodeChange" "FixedCodeChange.patch"
  apply_variant "FlakyCodeChange" "FlakyCodeChange.patch"
fi

# STEP 2 — start a setup-only container. Claude is not launched in this
# container: it temporarily sees the private reference trees solely so the
# launcher can capture the deterministic failure trace.
echo "[step 2 ] Starting setup container '$CONTAINER' from image '$IMAGE'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# STEP 3 — Run FlakyCodeChange to capture failure log.
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

echo "[step 3 ] /app/work/FlakyCodeChange -> /app/work/traces-flakycc (failure log)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flakycc; mkdir -p /app/work/traces-flakycc
  cd /app/work/FlakyCodeChange
  mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS
  mvn surefire:test \
    -pl $MODULE -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-flakycc/mvn.log || true
"

# Sanity (HARD GATE): TD FlakyCodeChange MUST reproduce the failure
# deterministically. The forced-verify oracle merges the agent's fix WITH this
# forcing, so if the forcing itself does not fail there is nothing to
# discriminate against -> abort instead of silently scoring a non-discriminative
# PASSED later. Mirrors run_agentic_od.sh's hard gate.
echo "[sanity ] Verifying FlakyCodeChange produced a test failure"
SUMMARY=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
            "$DATA_DIR/traces-flakycc/mvn.log" 2>/dev/null | tail -1 || true)
if [[ -z "$SUMMARY" ]]; then
  echo "ERROR: no Surefire summary in traces-flakycc/mvn.log — FlakyCodeChange did not run; cannot reproduce the TD failure."
  exit 1
fi
TESTS=$(  sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$SUMMARY"); TESTS=${TESTS:-0}
FAILURES=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$SUMMARY"); FAILURES=${FAILURES:-0}
ERRORS=$(  sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$SUMMARY"); ERRORS=${ERRORS:-0}
echo "[sanity ] FlakyCodeChange: Tests=$TESTS Failures=$FAILURES Errors=$ERRORS"
if (( TESTS < 1 )); then
  echo "ERROR: FlakyCodeChange ran 0 tests (Tests=$TESTS) — TD failure not reproduced."; exit 1
fi
if (( FAILURES + ERRORS < 1 )); then
  echo "ERROR: FlakyCodeChange did NOT fail (Failures=$FAILURES Errors=$ERRORS) — TD flakiness not reproduced from FlakyCodeChange; refusing to score a run whose forcing does not fail."; exit 1
fi

mkdir -p "$CLAUDE_INPUTS_DIR" "$CLAUDE_OUTPUTS_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$CLAUDE_INPUTS_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "td",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": false
}
JSONEOF

# The repair agent must never see Fixed/, FixedCodeChange/, ground-truth patch
# files, or issue-description artifacts. Stop the setup container and restart
# with three narrow mounts: its editable checkout, read-only prompt/harness
# inputs, and writable outputs. The Maven cache remains shared.
echo "[step 10 ] Restarting '$CONTAINER' with a ground-truth-free mount set"
docker exec -u 0 "$CONTAINER" chown -R "$(id -u):$(id -g)" \
  /app/work/FlakyCodeChange /app/work/traces-flakycc >/dev/null 2>&1 || true
docker rm -f "$CONTAINER" >/dev/null
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR/Flaky",target=/app/work/Flaky \
  --mount type=bind,source="$CLAUDE_INPUTS_DIR",target=/app/work/claude_inputs,readonly \
  --mount type=bind,source="$CLAUDE_OUTPUTS_DIR",target=/app/work/claude_outputs \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# AGENT
  echo "[agent ] launching agentic_claude_cli.py (Claude Code agent, model=${AGENTIC_MODEL:-claude-sonnet-4-6})"
  set +e
  "${AGENTIC_PYTHON:-python3}" "$SCRIPT_DIR/agentic_claude_cli.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --model "${AGENTIC_MODEL:-claude-sonnet-4-6}" \
    ${MAX_BUDGET_USD:+--max-budget-usd "$MAX_BUDGET_USD"} \
    ${VERIFY_PASS_RUNS:+--verify-pass-runs "$VERIFY_PASS_RUNS"} \
    ${CLI_TIMEOUT_S:+--cli-timeout-s "$CLI_TIMEOUT_S"}
  AGENT_RC=$?
  set -e

cleanup_completed_source_dirs() {
  local verdict=""
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/run_verdict.txt")"
  elif [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
    verdict="$(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi

  if [[ "$verdict" == "PASSED" || "$verdict" == "FAILED" ]]; then
    echo "[cleanup] removing completed-run source dirs: Fixed FixedCodeChange Flaky Flakym2 FlakyCodeChange"
    if command -v docker >/dev/null 2>&1; then
      docker exec -u 0 "$CONTAINER" chown -R "$(id -u):$(id -g)" \
        /app/work/Flaky /app/work/claude_outputs /root/.m2 >/dev/null 2>&1 || true
    fi
    rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/FixedCodeChange" \
           "$DATA_DIR/Flaky" "$DATA_DIR/Flakym2" \
           "$DATA_DIR/FlakyCodeChange" || \
      echo "[cleanup] WARNING: failed to remove one or more source dirs" >&2
  fi
}
cleanup_completed_source_dirs

rm -rf "$DATA_DIR/Flaky.pristine"

echo
echo "=========================================="
echo "[AGENTIC TD] Done."
for f in run_summary.csv trace_config.json rv_trace_diff.log llm_trace_summary.txt llm_context.txt \
         llm_response.json apply_report.json verify_after_fix.log \
         verify_after_fix.verdict verify_after_fix.result.json run_verdict.txt \
         td_validation/aggregate.json td_validation/calibration.json \
         td_validation/composition.json agentic_conversation.json \
         agentic_iterations.jsonl; do
  if [[ -f "$STEPS_OUT_DIR/$f" ]]; then
    sz=$(wc -c < "$STEPS_OUT_DIR/$f" | tr -d ' ')
    printf "  %-30s  %s bytes\n" "$f" "$sz"
  fi
done
if [[ -f "$STEPS_OUT_DIR/verify_after_fix.verdict" ]]; then
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/run_verdict.txt")   (verification: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null))"
  else
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi
fi
echo "=========================================="
exit $AGENT_RC
