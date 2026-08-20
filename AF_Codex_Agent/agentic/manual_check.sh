#!/usr/bin/env bash
# ============================================================
# manual_check.sh — check a fix by hand.
#
#   ./manual_check.sh <container>
#
#   1. expand
#   2. run the test without the fix
#   3. you apply the fix
#   4. run the test again
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CSV="$ROOT/test_config.csv"
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

C="${1:-}"
[[ -n "$C" ]] || { sed -n '2,12p' "$0"; exit 1; }
ROW=$(awk -F',' -v c="$C" '$1=="td" && $2==c {print; exit}' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$C' is not a td row in $CSV"; exit 1; }
IFS=',' read -r _ _ ZIP MODULE _ VICTIM _ _ JAVA _ URL <<< "$ROW"
MODULE=${MODULE:-.}
W="$ROOT/data/td_manual/$C"

ask() {
  local a
  while true; do
    read -r -p "$1 (yes/no): " a
    case "$(printf '%s' "$a" | tr 'A-Z' 'a-z')" in
      y|yes) return 0 ;;
      n|no|q|quit) return 1 ;;
      *) echo "  yes or no" ;;
    esac
  done
}

# ---------------------------------------------------------------- 1. EXPAND
echo
echo "=============================================================="
echo " 1. EXPAND"
echo "=============================================================="
mkdir -p "$W"
if [[ ! -d "$W/Flaky" ]]; then
  if [[ ! -f "$ROOT/data/$ZIP.zip" ]]; then
    echo "  downloading $ZIP.zip"
    curl -fL --retry 2 "$URL" -o "$ROOT/data/$ZIP.zip" || { echo "  download failed"; exit 1; }
  fi
  echo "  unzipping $ZIP.zip"
  unzip -oq "$ROOT/data/$ZIP.zip" -d "$W"
  [[ -d "$W/$ZIP" ]] && { mv "$W/$ZIP"/* "$W/" 2>/dev/null; rmdir "$W/$ZIP" 2>/dev/null; }
fi
# Always rebuild the working tree, so step 2 really is "without the fix".
rm -rf "$W/FlakyCodeChange"
cp -r "$W/Flaky" "$W/FlakyCodeChange"
patch -p1 -d "$W/FlakyCodeChange" < "$W/FlakyCodeChange.patch" >/dev/null 2>&1
echo "  built $W/FlakyCodeChange"

CLS="${VICTIM%%#*}"
VREL=$(cd "$W/Flaky" && find . -path '*/src/test/*' -name "${CLS##*.}.java" 2>/dev/null | head -1 | sed 's|^\./||')
[[ -n "$VREL" ]] || VREL="src/test/java/$(printf '%s' "$CLS" | tr '.' '/').java"
echo "  test : $VICTIM"
echo "  file : $W/FlakyCodeChange/$VREL"

IMG=flaky_base_jdk8
[[ "$JAVA" == 11 ]] && IMG=flaky_base_jdk11
[[ "$JAVA" == 17 ]] && IMG=flaky_base_jdk17
[[ "$(printf '%s' "$MODULE" | tr 'A-Z' 'a-z')" == *hadoop* ]] && IMG=flaky_base_jdk8_hadoop
CN="manual_$(printf '%s' "$C" | tr -c 'a-zA-Z0-9' '_')"
mkdir -p "$W/Flakym2/.m2"
CREATED=0
if [[ "$(docker inspect -f '{{.State.Running}}' "$CN" 2>/dev/null)" != "true" ]]; then
  docker rm -f "$CN" >/dev/null 2>&1
  docker run -d --platform linux/amd64 --name "$CN" \
    --mount "type=bind,source=$W,target=/app/work" \
    --mount "type=bind,source=$W/Flakym2/.m2,target=/root/.m2" \
    "$IMG" tail -f /dev/null >/dev/null || { echo "docker run failed"; exit 1; }
  CREATED=1
fi
trap '(( CREATED )) && docker rm -f "$CN" >/dev/null 2>&1' EXIT

run_test() {
  echo "  running ..."
  local out rc
  out=$(docker exec "$CN" bash -c "
    cd /app/work/FlakyCodeChange
    mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS >/dev/null 2>&1
    mvn surefire:test -pl $MODULE -Dtest='$VICTIM' $MVNOPTS 2>&1" 2>&1)
  rc=$?
  if ! printf '%s' "$out" | grep -q "Tests run:"; then
    echo "    ERROR: the test did not run (rc=$rc)"
    printf '%s\n' "$out" | head -5 | sed 's/^/    /'
    return 1
  fi
  printf '%s\n' "$out" \
    | grep -E "Tests run:|<<< (FAILURE|ERROR)|BUILD (SUCCESS|FAILURE)|COMPILATION ERROR" \
    | sed 's/^/    /' | tail -12
}

# ------------------------------------------------- 2. RUN WITHOUT THE FIX
echo
echo "=============================================================="
echo " 2. RUN THE TEST — no fix applied"
echo "=============================================================="
ask "  run it?" && run_test
echo
echo "  expected if flaky: Failures: 1 (or Errors: 1)"

# ------------------------------------------------------- 3. APPLY THE FIX
echo
echo "=============================================================="
echo " 3. APPLY YOUR FIX"
echo "=============================================================="
echo "  edit: $W/FlakyCodeChange/$VREL"
echo
while ! ask "  applied?"; do :; done

# ---------------------------------------------------- 4. RUN WITH THE FIX
echo
echo "=============================================================="
echo " 4. RUN THE TEST — fix applied"
echo "=============================================================="
while true; do
  run_test
  echo
  echo "  Failures: 0 -> flakiness gone     Failures: 1 -> still there"
  echo
  ask "  run again?" || break
done

echo
echo "tree: $W"
