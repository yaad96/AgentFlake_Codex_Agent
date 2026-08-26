#!/usr/bin/env bash
# ============================================================
# run_agentic_nio.sh — agentic NIO repair pipeline
#
# Mirrors run_nio_tracemop.sh's setup (steps 1-7 plus the auto-generated
# JUnit wrapper that re-invokes the victim twice in one JVM) but replaces
# steps 8-11 with a call to agentic_codex_cli.py. The Codex CLI agent then
# iterates through context tools up to the configured Codex turn cap, using the wrapper-based verify command provided by agentic_verify.py.
#
# Usage:  ./run_agentic_nio.sh <result_container> [options]
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
IFS=',' read -r TEST_TYPE _RC ZIP MODULE POLLUTER VICTIM ITERATIONS CONFIG JAVA NONDEX URL <<< "$ROW"

if [[ "$TEST_TYPE" != "nio" ]]; then
  echo "ERROR: this script targets nio only; got '$TEST_TYPE'."; exit 1
fi
if [[ -z "$VICTIM" ]]; then
  echo "ERROR: NIO container '$RESULT_CONTAINER' must have a victim test in CSV."; exit 1
fi

# Derive wrapper identifiers exactly the way run_nio_tracemop.sh does.
VICTIM_CLASS_FULL="${VICTIM%#*}"
VICTIM_METHOD="${VICTIM##*#}"
VICTIM_CLASS_SIMPLE="${VICTIM_CLASS_FULL##*.}"
VICTIM_PKG="${VICTIM_CLASS_FULL%.*}"
VICTIM_PKG_PATH="$(echo "$VICTIM_PKG" | tr '.' '/')"
METHOD_CAP="$(printf '%s' "${VICTIM_METHOD:0:1}" | tr '[:lower:]' '[:upper:]')${VICTIM_METHOD:1}"
WRAPPER_CLASS_SIMPLE="${METHOD_CAP}NioReproTest"
WRAPPER_FQCN="${VICTIM_PKG}.${WRAPPER_CLASS_SIMPLE}"
WRAPPER_PATH_REL="${MODULE}/src/test/java/${VICTIM_PKG_PATH}/${WRAPPER_CLASS_SIMPLE}.java"

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
[AGENTIC NIO]
result_container : $RESULT_CONTAINER
victim           : $VICTIM
wrapper          : $WRAPPER_FQCN
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
    mkdir -p "$DATA_DIR"
    unzip -o "$ZIP_PATH" -d "$DATA_DIR" >/dev/null
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
fi

# Preflight: victim method must exist in resolved source.
VICTIM_FILE_REL="${MODULE}/src/test/java/${VICTIM_PKG_PATH}/${VICTIM_CLASS_SIMPLE}.java"
VICTIM_FILE_ABS="$DATA_DIR/Fixed/$VICTIM_FILE_REL"
if [[ ! -f "$VICTIM_FILE_ABS" ]]; then
  echo "ERROR: victim source file not found at $VICTIM_FILE_REL"; exit 1
fi
if ! grep -qwF "$VICTIM_METHOD" "$VICTIM_FILE_ABS"; then
  echo "ERROR: victim method '$VICTIM_METHOD' not in $VICTIM_FILE_REL"; exit 1
fi

# Detect surefire version pinned by the project (single-property resolution
# only — sufficient for the agentic case, matches run_nio_tracemop.sh's logic
# for the common case).
SUREFIRE_VER=$(awk '
  /<plugin>/,/<\/plugin>/ {
    if (/maven-surefire-plugin/) found=1
    if (found && /<version>/) {
      sub(/.*<version>/, "")
      sub(/<\/version>.*/, "")
      gsub(/[[:space:]]/, "")
      print
      exit
    }
    if (/<\/plugin>/) found=0
  }
' "$DATA_DIR/Flaky/pom.xml" 2>/dev/null)
PROP_RX='^\$\{(.+)\}$'
for _ in 1 2 3; do
  [[ "$SUREFIRE_VER" =~ $PROP_RX ]] || break
  prop_name="${BASH_REMATCH[1]}"
  esc_prop="${prop_name//./\\.}"
  resolved=$(find "$DATA_DIR/Flaky" -maxdepth 8 -name pom.xml -print0 2>/dev/null \
    | xargs -0 grep -h "<$prop_name>" 2>/dev/null \
    | sed -nE "s|.*<${esc_prop}>([^<]+)</${esc_prop}>.*|\1|p" \
    | head -n 1 | sed -E 's/^[[:space:]]+|[[:space:]]+$//g')
  [[ -z "$resolved" ]] && { SUREFIRE_VER=""; break; }
  SUREFIRE_VER="$resolved"
done
[[ -z "$SUREFIRE_VER" ]] && SUREFIRE_VER="3.0.0-M5"
echo "[step 1c] Surefire version: $SUREFIRE_VER"

# STEP 2 — start container
echo "[step 2 ] Starting container '$CONTAINER'"
docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
mkdir -p "$DATA_DIR/Flakym2/.m2"
docker run -d "${DOCKER_PLATFORM_ARGS[@]}" --name "$CONTAINER" \
  --mount type=bind,source="$DATA_DIR",target=/app/work \
  --mount type=bind,source="$DATA_DIR/Flakym2/.m2",target=/root/.m2 \
  "$IMAGE" tail -f /dev/null >/dev/null

# STEP 3 — generate wrapper class in BOTH Fixed/ and Flaky/
echo "[step 3 ] Generating NIO wrapper at $WRAPPER_PATH_REL"
gen_wrapper() {
  local root="$1"
  local out="$root/$WRAPPER_PATH_REL"
  mkdir -p "$(dirname "$out")"
  cat > "$out" <<EOF
package ${VICTIM_PKG};

// AUTO-GENERATED by run_agentic_nio.sh — DO NOT EDIT.
// NIO repro driver: invokes ${VICTIM_CLASS_SIMPLE}#${VICTIM_METHOD} twice in
// the same JVM (full JUnit lifecycle each time). Fix target is the victim,
// NOT this file.

import org.junit.Test;
import org.junit.Assert;
import org.junit.runner.JUnitCore;
import org.junit.runner.Request;
import org.junit.runner.Result;

public class ${WRAPPER_CLASS_SIMPLE} {
    @Test public void runTwice() throws Exception {
        Request req = Request.method(${VICTIM_CLASS_SIMPLE}.class, "${VICTIM_METHOD}");
        Result r1 = new JUnitCore().run(req);
        Assert.assertTrue("first invocation should pass: " + r1.getFailures(), r1.wasSuccessful());
        Result r2 = new JUnitCore().run(req);
        Assert.assertTrue("second invocation should pass (NIO assertion): " + r2.getFailures(), r2.wasSuccessful());
    }
}
EOF
}
gen_wrapper "$DATA_DIR/Fixed"
gen_wrapper "$DATA_DIR/Flaky"

# STEP 4 — Run Fixed+wrapper and Flaky+wrapper to capture logs.
MVNOPTS='-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false -Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip -Dmaven.javadoc.skip -Dwarbucks.skip -Dmodernizer.skip -Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip -Dcobertura.skip=true -Dfindbugs.skip=true -Dspotless.skip=true -Dspotless.check.skip=true -Dossindex.skip=true -Dmaven.bundle.plugin.skip=true -Dmaven.parallel.force=false -Ddisable.checks=true'

echo "[step 4 ] /app/work/Fixed + wrapper -> /app/work/traces-fixed (sanity)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-fixed; mkdir -p /app/work/traces-fixed
  export SUREFIRE_VERSION=$SUREFIRE_VER
  cd /app/work/Fixed
  mvn install -Dmaven.test.skip=true -pl $MODULE -am -q $MVNOPTS
  mvn test \
    -pl $MODULE -am \
    -Dtest='${WRAPPER_FQCN}#runTwice' \
    $MVNOPTS 2>&1 | tee /app/work/traces-fixed/mvn.log || true
"

echo "[step 4 ] /app/work/Flaky + wrapper -> /app/work/traces-flaky (failure log)"
docker exec "$CONTAINER" bash -c "
  set -e
  rm -rf /app/work/traces-flaky; mkdir -p /app/work/traces-flaky
  export SUREFIRE_VERSION=$SUREFIRE_VER
  cd /app/work/Flaky
  mvn install -Dmaven.test.skip=true -pl $MODULE -am -q $MVNOPTS
  mvn test \
    -pl $MODULE -am \
    -Dtest='${WRAPPER_FQCN}#runTwice' \
    $MVNOPTS 2>&1 | tee /app/work/traces-flaky/mvn.log || true
"

# Sanity: Fixed+wrapper PASSED and Flaky+wrapper FAILED.
parse_summary() {
  local sum t f e
  sum=$(grep -E "Tests run:[[:space:]]+[0-9]+,[[:space:]]+Failures:[[:space:]]+[0-9]+,[[:space:]]+Errors:[[:space:]]+[0-9]+" \
          "$1" 2>/dev/null | tail -1 || true)
  if [[ -z "$sum" ]]; then echo "0 0 0"; return; fi
  t=$(sed -nE 's/.*Tests run:[[:space:]]+([0-9]+).*/\1/p' <<<"$sum"); t=${t:-0}
  f=$(sed -nE 's/.*Failures:[[:space:]]+([0-9]+).*/\1/p'  <<<"$sum"); f=${f:-0}
  e=$(sed -nE 's/.*Errors:[[:space:]]+([0-9]+).*/\1/p'    <<<"$sum"); e=${e:-0}
  echo "$t $f $e"
}

read -r FT FF FE <<< "$(parse_summary "$DATA_DIR/traces-fixed/mvn.log")"
echo "[sanity ] Fixed+wrapper:  Tests=$FT Failures=$FF Errors=$FE"
# Fixed+wrapper is an OBSERVATION about the dataset's reference patch, not a
# statement about the agent. Whether the shipped Fixed.patch happens to satisfy
# "second invocation should pass" in this environment is independent of whether
# the agent can produce a patch that does. AgenticFlaky never runs this check
# and repairs several containers whose reference fix does not hold here, so
# gating on it scored this pipeline under a stricter rule than the tools it is
# compared against.
#
# It is still run and recorded (fixed_wrapper_ok in trace_config.json ->
# meta.json) so a run where the reference fix failed stays identifiable.
# The real gate remains Flaky+wrapper reproducing the NIO behaviour below.
if (( FT < 1 || FF + FE >= 1 )); then
  FIXED_WRAPPER_OK=false
  echo "[sanity ] WARNING: Fixed+wrapper did not pass cleanly (Tests=$FT Failures=$FF Errors=$FE)."
  echo "[sanity ]          The dataset's reference fix does not hold in this environment."
  echo "[sanity ]          Continuing: the agent is judged on its own patch, not this one."
else
  FIXED_WRAPPER_OK=true
fi
read -r KT KF KE <<< "$(parse_summary "$DATA_DIR/traces-flaky/mvn.log")"
echo "[sanity ] Flaky+wrapper:  Tests=$KT Failures=$KF Errors=$KE"
if (( KT < 1 || KF + KE < 1 )); then
  echo "ERROR: Flaky+wrapper did not exhibit NIO behaviour — bug not reproduced"; exit 1
fi
echo "[sanity ] OK — Flaky failed (NIO reproduced); fixed_wrapper_ok=$FIXED_WRAPPER_OK"

mkdir -p "$CODEX_INPUTS_DIR" "$CODEX_OUTPUTS_DIR"

# STEP 9.5 — snapshot
# Tests can leave root-owned files inside the bind mount (HBase's MiniDFSCluster
# writes build/test/data/dfs/data/* as root, for one), and the host-side cp below
# then dies with "Permission denied" -- aborting a run that had already
# reproduced the flake. The chown that already exists later in this script runs
# far too late to help, so reclaim ownership here, before the snapshot.
docker exec -u 0 "$CONTAINER" chown -R "$(id -u):$(id -g)" /app/work >/dev/null 2>&1 || true

echo "[step 9.5] snapshotting Flaky/ -> Flaky.pristine"
rm -rf "$DATA_DIR/Flaky.pristine"
cp -r "$DATA_DIR/Flaky" "$DATA_DIR/Flaky.pristine"

echo "[step 9.5] Writing trace_config.json"
cat > "$CODEX_INPUTS_DIR/trace_config.json" <<JSONEOF
{
  "docker_container": "$CONTAINER",
  "test_type": "nio",
  "module": "$MODULE",
  "polluter": "",
  "victim": "$VICTIM",
  "nondex_seed": "",
  "nondex_runs": 0,
  "wrapper_fqcn": "$WRAPPER_FQCN",
  "surefire_version": "$SUREFIRE_VER",
  "fixed_wrapper_ok": $FIXED_WRAPPER_OK,
  "tracemop_ready": false
}
JSONEOF

# AGENT — verify_victim for NIO needs WRAPPER_FQCN + SUREFIRE_VER in env;
# agentic_verify.py reads them, mirroring run_nio_tracemop.sh's verify_victim().
export WRAPPER_FQCN SUREFIRE_VER
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
echo "[AGENTIC NIO] Done."
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
