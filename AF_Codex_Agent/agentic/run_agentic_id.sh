#!/usr/bin/env bash
# ============================================================
# run_agentic_id.sh — agentic ID repair pipeline
#
# Mirrors run_id_tracemop.sh's setup (steps 1-7) but replaces steps
# 8-11 with a call to agentic_codex_cli.py. The Codex CLI agent then iterates
# through context tools up to the configured Codex turn cap.
#
# Usage:  ./run_agentic_id.sh <result_container> [options]
# Requires: the key in .openai_api_key (an exported OPENAI_API_KEY is ignored) + install AF_Codex_Agent/requirements.txt
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

# The key ALWAYS comes from the key file. An exported OPENAI_API_KEY is
# ignored and overwritten here: a stale export silently shadowing the file
# made every run bill an exhausted account while the file (and every manual
# curl test) used a working one.
if [[ -f "$OPENAI_API_KEY_FILE" ]]; then
  _file_key="$(sed -n "s/^[[:space:]]*//; s/[[:space:]]*$//; /^[#]/d; /^$/d; p; q" "$OPENAI_API_KEY_FILE")"
  if [[ -n "${OPENAI_API_KEY:-}" && "${OPENAI_API_KEY:-}" != "$_file_key" ]]; then
    echo "[setup] NOTE: exported OPENAI_API_KEY ignored; using $OPENAI_API_KEY_FILE"
  fi
  OPENAI_API_KEY="$_file_key"
  export OPENAI_API_KEY
  unset _file_key
fi

if [[ -z "${OPENAI_API_KEY:-}" ]]; then
  echo "ERROR: no API key in $OPENAI_API_KEY_FILE. Exporting OPENAI_API_KEY will NOT work -- it is ignored by design."; exit 1
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

[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }
ROW=$(awk -F',' -v rc="$RESULT_CONTAINER" '$2 == rc { print; exit }' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$RESULT_CONTAINER' not in $CSV"; exit 1; }
ROW="${ROW%$'\r'}"  # strip trailing CR if CSV has CRLF endings
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEXSEED URL <<< "$ROW"

if [[ "$TEST_TYPE" != "id" ]]; then
  echo "ERROR: this script targets id only; got '$TEST_TYPE'."; exit 1
fi
if [[ -z "$NONDEXSEED" ]]; then
  echo "ERROR: ID container '$RESULT_CONTAINER' must have a NonDex seed in CSV."; exit 1
fi

case "$JAVA" in
  8)  IMAGE="flaky_base_jdk_8_id_cover_new";  DOCKERFILE="Dockerfile8.id" ;;
  11) IMAGE="flaky_base_jdk_11_id_cover_new"; DOCKERFILE="Dockerfile11.id" ;;
  17) IMAGE="flaky_base_jdk_17_id_cover_new"; DOCKERFILE="Dockerfile17.id" ;;
  *)  echo "ERROR: unsupported java=$JAVA"; exit 1 ;;
esac
PROJECT_KEY="$(printf '%s\n' "$MODULE" | tr '[:upper:]' '[:lower:]')"
if [[ "$PROJECT_KEY" == *hadoop* ]]; then
  IMAGE="flaky_base_jdk8_hadoop"
  DOCKERFILE="Dockerfile.hadoop"
fi
# NonDex version matters. Older releases can fail to compile a project's test
# sources, which shows up as maven-compiler-plugin testCompile failing on every
# NonDex attempt with no Surefire summary at all -- observed on crane4j-core
# under 2.1.1, while FlakyDoctor builds the same subjects fine on 2.1.7.
# An explicit NONDEX_PLUGIN_VERSION in the environment always wins, so a single
# container can be retried on a different version without changing the default
# for an already-completed batch.
_NDX_ENV="${NONDEX_PLUGIN_VERSION:-}"
NONDEX_PLUGIN_VERSION="${_NDX_ENV:-2.1.1}"
if [[ "$JAVA" == "17" && -z "$_NDX_ENV" ]]; then
  NONDEX_PLUGIN_VERSION="2.1.7"
fi
unset _NDX_ENV

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
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  return $rc
}
trap cleanup_container EXIT

cat <<EOF
==========================================
[AGENTIC ID]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
nondex seed      : $NONDEXSEED
java             : $JAVA  (image: $IMAGE)
container        : $CONTAINER
==========================================
EOF

# STEP 0 — cleanup
if [[ -d "$DATA_DIR/Fixed" || -d "$DATA_DIR/Flaky" || -d "$DATA_DIR/Flakym2" || -d "$DATA_DIR/Flaky.pristine" || -d "$DATA_DIR/result" ]]; then
  echo "[step 0 ] Cleaning mutated source dirs from previous run"
  rm -rf "$DATA_DIR/Fixed" "$DATA_DIR/Flaky" "$DATA_DIR/Flakym2" \
         "$DATA_DIR/Flaky.pristine" "$DATA_DIR/result"
fi

# STEP 1 — unzip + Fixed.patch
need_step1=0
for d in Fixed Flaky Flakym2; do [[ -d "$DATA_DIR/$d" ]] || need_step1=1; done
if (( need_step1 )); then
  ZIP_PATH="$REPROFLAKE_DIR/data/${ZIP}.zip"
  # A cached archive is only trustworthy if it is actually intact. A partial
  # download, or a file evicted by cloud sync (macOS iCloud marks these
  # "dataless" and a read can return nothing), leaves a plausible-looking
  # ZIP_PATH that unzip then rejects -- and because the archive is cached by
  # existence alone, that poisons EVERY later run of this container until it
  # is removed by hand. Verify and re-fetch instead.
  if [[ -f "$ZIP_PATH" ]] && ! unzip -t "$ZIP_PATH" >/dev/null 2>&1; then
    echo "[step 1a] cached archive is corrupt or unreadable, re-downloading: $ZIP_PATH"
    rm -f "$ZIP_PATH"
  fi
  if [[ ! -f "$ZIP_PATH" ]]; then
    [[ -n "$URL" ]] || { echo "ERROR: $ZIP_PATH not found and URL empty"; exit 1; }
    mkdir -p "$REPROFLAKE_DIR/data"
    if   command -v curl >/dev/null; then curl -fL "$URL" -o "$ZIP_PATH"
    elif command -v wget >/dev/null; then wget "$URL" -O "$ZIP_PATH"
    else echo "ERROR: need curl or wget"; exit 1; fi
  fi
  if [[ ! -d "$DATA_DIR/Flaky" || ! -d "$DATA_DIR/Flakym2" ]]; then
    echo "[step 1a] Unzipping $ZIP_PATH"
    mkdir -p "$DATA_DIR"; unzip -o "$ZIP_PATH" -d "$DATA_DIR" >/dev/null
    if [[ -d "$DATA_DIR/$ZIP" ]]; then
      mv "$DATA_DIR/$ZIP/"* "$DATA_DIR/" 2>/dev/null || true
      rmdir "$DATA_DIR/$ZIP" 2>/dev/null || true
    fi
  fi
  if [[ ! -d "$DATA_DIR/Fixed" ]]; then
    [[ -f "$DATA_DIR/Fixed.patch" ]] || { echo "ERROR: $DATA_DIR/Fixed.patch missing"; exit 1; }
    echo "[step 1b] Creating Fixed/ = Flaky/ + Fixed.patch (evaluation only)"
    cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Fixed"
    patch -p1 -d "$DATA_DIR/Fixed" < "$DATA_DIR/Fixed.patch" >/dev/null
  fi
fi

# STEP 2 — start container
echo "[step 2 ] Starting container '$CONTAINER'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
M2_MOUNT_ARGS=()
if [[ -d "$DATA_DIR/Flakym2/.m2" ]]; then
  M2_MOUNT_ARGS=(--mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2)
fi
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  "${M2_MOUNT_ARGS[@]}" \
  "$IMAGE" tail -f /dev/null >/dev/null

MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false -Ddisable.checks=true'

NONDEX_RUNS="$ITERATIONS"
if (( NONDEX_RUNS > 10 )); then
  echo "[step 4d] capping NonDex runs at 10 (CSV says $ITERATIONS)"
  NONDEX_RUNS=10
fi

# Pre-build
PREBUILD_SKIP_ARG="-Dmaven.test.skip=true"
PREBUILD_TARGET_ARGS="-pl '$MODULE' -am"
if [[ "$PROJECT_KEY" == *flink* ]]; then
  PREBUILD_SKIP_ARG="-DskipTests"
  PREBUILD_TARGET_ARGS="-pl flink-runtime,flink-test-utils-parent/flink-test-utils,'$MODULE' -am"
fi
echo "[step 4d] pre-build: mvn install $PREBUILD_SKIP_ARG"
docker exec "$CONTAINER" bash -c "
  set -e
  cd /app/work/Flaky
  mvn install $PREBUILD_SKIP_ARG $PREBUILD_TARGET_ARGS -q $MVNOPTS
"

# Run #1: traces-pass (plain mvn test)
echo "[step 3 ] /app/work/Flaky -> /app/work/traces-pass"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-pass; mkdir -p /app/work/traces-pass
  cd /app/work/Flaky
  mvn test \
    -pl '$MODULE' -Dtest='$VICTIM' \
    $MVNOPTS 2>&1 | tee /app/work/traces-pass/mvn.log || true
"

# Run #2: traces-fail (NonDex with seed; captures failure log)
echo "[step 3 ] /app/work/Flaky -> /app/work/traces-fail (NonDex seed=$NONDEXSEED max-runs=$NONDEX_RUNS)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fail; mkdir -p /app/work/traces-fail
  cd /app/work/Flaky
  : > /app/work/traces-fail/mvn.log
  python3 - <<'PY' > /app/work/traces-fail/seeds.txt
seed = int('$NONDEXSEED')
mask = (1 << 48) - 1
mult = 0x5DEECE66D
add = 0xB
state = (seed ^ mult) & mask
print(seed)
for _ in range(1, int('$NONDEX_RUNS')):
    # Java Random.next(32) gives a 32-bit signed int seed. NonDex nondexSeed is
    # an int parameter, so 64-bit longs overflow it. Keep fallback seeds within
    # int range -- valid for NonDex 2.1.1 and 2.1.7.
    state = (state * mult + add) & mask
    val = state >> 16
    if val >= (1 << 31):
        val -= 1 << 32
    print(val)
PY
  i=0
  while IFS= read -r seed; do
    i=\$((i + 1))
    echo \"[nondex] attempt \$i/$NONDEX_RUNS seed=\$seed\" | tee -a /app/work/traces-fail/mvn.log
    mvn edu.illinois:nondex-maven-plugin:$NONDEX_PLUGIN_VERSION:nondex \
      -DnondexSeed=\$seed -DnondexRuns=1 \
      -pl '$MODULE' -Dtest='$VICTIM' \
      $MVNOPTS 2>&1 | tee -a /app/work/traces-fail/mvn.log || true
    if tail -n 200 /app/work/traces-fail/mvn.log | grep -Eq 'Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[1-9][0-9]*|Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[1-9][0-9]*'; then
      echo \"\$seed\" > /app/work/traces-fail/failing_seed
      break
    fi
  done < /app/work/traces-fail/seeds.txt
"

if [[ -f "$DATA_DIR/traces-fail/failing_seed" ]]; then
  NONDEXSEED="$(cat "$DATA_DIR/traces-fail/failing_seed")"
  NONDEX_RUNS=1
  echo "[step 4d] using reproduced failing NonDex seed=$NONDEXSEED"
fi

# Sanity: at least one NonDex iteration must have failed.
echo "[sanity ] Verifying at least one NonDex iteration failed"
ITER_SUMMARIES=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
                  "$DATA_DIR/traces-fail/mvn.log" 2>/dev/null || true)
if [[ -z "$ITER_SUMMARIES" ]]; then
  echo "ERROR: no Surefire summary in traces-fail/mvn.log"; exit 1
fi
TOTAL_TESTS=0; TOTAL_FAIL=0; TOTAL_ERR=0; FAIL_ITERS=0
while IFS= read -r line; do
  [[ -z "$line" ]] && continue
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$line"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$line"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$line"); e=${e:-0}
  TOTAL_TESTS=$((TOTAL_TESTS + t)); TOTAL_FAIL=$((TOTAL_FAIL + f)); TOTAL_ERR=$((TOTAL_ERR + e))
  (( f + e >= 1 )) && FAIL_ITERS=$((FAIL_ITERS + 1))
done <<< "$ITER_SUMMARIES"
echo "[sanity ] Totals: Tests=$TOTAL_TESTS Failures=$TOTAL_FAIL Errors=$TOTAL_ERR  (failing iters=$FAIL_ITERS)"
if (( TOTAL_TESTS < 1 )); then echo "ERROR: NonDex executed 0 tests"; exit 1; fi
if (( TOTAL_FAIL + TOTAL_ERR < 1 )); then
  echo "ERROR: NonDex produced 0 failures across iterations — bug not reproduced"; exit 1
fi

mkdir -p "$CODEX_INPUTS_DIR" "$CODEX_OUTPUTS_DIR"

# STEP 9.5 — snapshot
echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$CODEX_INPUTS_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "id",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "$NONDEXSEED",
  "nondex_runs": $NONDEX_RUNS,
  "nondex_plugin_version": "$NONDEX_PLUGIN_VERSION",
  "wrapper_fqcn": "",
  "surefire_version": "",
  "tracemop_ready": false
}
JSONEOF

# AGENT — verify_victim for ID needs NONDEXSEED + NONDEX_RUNS in env;
# agentic_verify.py reads them, mirroring run_id_tracemop.sh's verify_victim().
export NONDEXSEED NONDEX_RUNS NONDEX_PLUGIN_VERSION
  echo "[agent ] launching agentic_codex_cli.py (Codex agent, model=${AGENTIC_MODEL:-gpt-5.4})"
  set +e
  "${AGENTIC_PYTHON:-python3}" "$SCRIPT_DIR/agentic_codex_cli.py" "$RESULT_CONTAINER" \
    --docker-container "$CONTAINER" \
    --model "${AGENTIC_MODEL:-gpt-5.4}" \
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
echo "[AGENTIC ID] Done."
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
  if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/run_verdict.txt")   (verification: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null))"
  else
    echo "Final verdict: $(cat "$STEPS_OUT_DIR/verify_after_fix.verdict")"
  fi
fi
echo "=========================================="
exit $AGENT_RC
