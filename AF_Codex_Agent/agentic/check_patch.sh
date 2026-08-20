#!/usr/bin/env bash
# ============================================================
# check_patch.sh — does a patch actually remove the flakiness?
#
#   ./check_patch.sh <container> [options]
#
# Runs the victim test in the tree where it fails EVERY time, once before the
# patch and once after. BEFORE must FAIL and AFTER must PASS; anything else and
# the patch did not fix it.
#
# Options
#   --run run_NN     use data/<container>/<run_NN>/claude_outputs/patch.diff
#                    (default: the newest run that has a non-empty patch.diff)
#   --patch FILE     use an explicit patch instead (e.g. the developer's fix,
#                    or one you wrote yourself)
#   --dev            use the developer's fix, extracted from the dataset
#   --repeat N       run the test N times on each side (default 1). These are
#                    TIMING tests: one PASS can be luck. Use 5 for anything you
#                    intend to report.
#   --reset          discard edits and rebuild the flaky tree from scratch
#   --restage        re-download / re-unzip the container from the dataset zip
#   --keep           leave the docker container running afterwards so you can
#                    iterate by hand (the script prints the commands)
#
# Examples
#   ./check_patch.sh COLLECTIONS-812 --run run_02 --repeat 5
#   ./check_patch.sh APEXCORE-403 --dev --repeat 5
#   ./check_patch.sh CURATOR-681 --patch /tmp/my_attempt.diff --keep
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CSV="$ROOT/test_config.csv"
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'

CONTAINER=""; RUN=""; PATCH=""; REPEAT=1; RESET=0; RESTAGE=0; KEEP=0; USEDEV=0
while (( $# )); do
  case "$1" in
    --run)     RUN="$2"; shift 2 ;;
    --patch)   PATCH="$2"; shift 2 ;;
    --dev)     USEDEV=1; shift ;;
    --repeat)  REPEAT="$2"; shift 2 ;;
    --reset)   RESET=1; shift ;;
    --restage) RESTAGE=1; shift ;;
    --keep)    KEEP=1; shift ;;
    -h|--help) sed -n '2,32p' "$0"; exit 0 ;;
    *)         CONTAINER="$1"; shift ;;
  esac
done
[[ -n "$CONTAINER" ]] || { sed -n '2,32p' "$0"; exit 1; }
[[ -f "$CSV" ]] || { echo "ERROR: $CSV not found"; exit 1; }

ROW=$(awk -F',' -v c="$CONTAINER" '$1=="td" && $2==c {print; exit}' "$CSV")
[[ -n "$ROW" ]] || { echo "ERROR: '$CONTAINER' is not a td row in test_config.csv"; exit 1; }
IFS=',' read -r _ _ ZIP MODULE _ VICTIM _ _ JAVA _ URL <<< "$ROW"
MODULE=${MODULE:-.}
W="$HOME/td_manual/$CONTAINER"

# ---------- 1. locate the patch ---------------------------------------------
if (( USEDEV )); then
  PATCH="$W/.devfix.diff"          # generated below, after staging
elif [[ -z "$PATCH" ]]; then
  if [[ -n "$RUN" ]]; then
    PATCH="$ROOT/data/$CONTAINER/$RUN/claude_outputs/patch.diff"
  else
    for d in $(ls -dt "$ROOT/data/$CONTAINER"/run_* 2>/dev/null); do
      if [[ -s "$d/claude_outputs/patch.diff" ]]; then PATCH="$d/claude_outputs/patch.diff"; break; fi
    done
  fi
fi

# ---------- 2. stage the flaky codebase --------------------------------------
if (( RESTAGE )); then rm -rf "$W"; fi
if [[ ! -d "$W/FlakyCodeChange" ]]; then
  echo "[stage] $CONTAINER -> $W"
  rm -rf "$W"; mkdir -p "$W"
  if [[ ! -f "$ROOT/data/$ZIP.zip" ]]; then
    echo "[stage] downloading $URL"
    curl -fL --retry 2 "$URL" -o "$ROOT/data/$ZIP.zip" || { echo "download failed"; exit 1; }
  fi
  unzip -oq "$ROOT/data/$ZIP.zip" -d "$W"
  [[ -d "$W/$ZIP" ]] && { mv "$W/$ZIP"/* "$W/" 2>/dev/null; rmdir "$W/$ZIP" 2>/dev/null; }
  for t in FlakyCodeChange Fixed; do
    cp -r "$W/Flaky" "$W/$t"
    patch -p1 -d "$W/$t" < "$W/$t.patch" >/dev/null 2>&1
  done
fi
if (( RESET )); then
  echo "[reset] rebuilding FlakyCodeChange/ from Flaky/ + FlakyCodeChange.patch"
  rm -rf "$W/FlakyCodeChange"; cp -r "$W/Flaky" "$W/FlakyCodeChange"
  patch -p1 -d "$W/FlakyCodeChange" < "$W/FlakyCodeChange.patch" >/dev/null 2>&1
fi
if (( USEDEV )); then
  diff -ruN --exclude=target --exclude=build "$W/Flaky" "$W/Fixed" > "$PATCH" 2>/dev/null
  if [[ ! -s "$PATCH" ]]; then
    echo "ERROR: this container's Fixed.patch contains NO source change --"
    echo "       there is no developer fix to test (e.g. BOOKKEEPER-709, tdwro4jcore1)."
    exit 2
  fi
fi
[[ -s "${PATCH:-}" ]] || { echo "ERROR: no patch found. Use --run/--patch/--dev."; exit 1; }
# Absolutise: the apply below runs inside $W/FlakyCodeChange, so a relative
# --patch path would silently fail to resolve there.
PATCH="$(cd "$(dirname "$PATCH")" && pwd)/$(basename "$PATCH")"

echo
echo "=========================================================================="
echo " 1. PATCH        $PATCH"
echo "                 $(wc -c < "$PATCH" | tr -d ' ') bytes, touches:"
grep '^+++ ' "$PATCH" | sed 's|^+++ [ab]/||;s|^+++ [^/]*/||;s|\t.*||' | sed 's/^/                   /'
echo " 2. PASTE INTO   $W/FlakyCodeChange/"
echo "                 (pristine + timing perturbation: the test fails here EVERY time)"
echo "                 NOT Flaky/ -- the test passes there anyway, proving nothing."
echo " 3. RUN          victim: $VICTIM"
echo "                 module: $MODULE"
echo "=========================================================================="

# ---------- 3. container ------------------------------------------------------
IMG=flaky_base_jdk8
[[ "$JAVA" == 11 ]] && IMG=flaky_base_jdk11
[[ "$JAVA" == 17 ]] && IMG=flaky_base_jdk17
[[ "$(printf '%s' "$MODULE" | tr 'A-Z' 'a-z')" == *hadoop* ]] && IMG=flaky_base_jdk8_hadoop
CN="check_$(printf '%s' "$CONTAINER" | tr -c 'a-zA-Z0-9' '_')"
if ! docker inspect "$CN" >/dev/null 2>&1; then
  mkdir -p "$W/Flakym2/.m2"
  docker run -d --platform linux/amd64 --name "$CN" \
    --mount "type=bind,source=$W,target=/app/work" \
    --mount "type=bind,source=$W/Flakym2/.m2,target=/root/.m2" \
    "$IMG" tail -f /dev/null >/dev/null || { echo "docker run failed"; exit 1; }
fi

run_victim() {   # $1 = label
  local i out line pass=0 fail=0
  for (( i=1; i<=REPEAT; i++ )); do
    out=$(docker exec "$CN" bash -c "
      cd /app/work/FlakyCodeChange
      mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS >/dev/null 2>&1
      mvn surefire:test -pl $MODULE -Dtest='$VICTIM' $MVNOPTS 2>&1")
    line=$(printf '%s' "$out" | grep -E "Tests run: [0-9]+, Failures" | tail -1)
    [[ -z "$line" ]] && line="(no tests ran -- check module/victim)"
    echo "    $1 run $i: $line"
    case "$line" in *"Failures: 0, Errors: 0"*) pass=$((pass+1)) ;; *) fail=$((fail+1)) ;; esac
  done
  echo "    $1 => $pass PASS / $fail FAIL of $REPEAT"
}

echo
echo "--- BEFORE the patch (must FAIL, or the setup is wrong)"
run_victim BEFORE

echo
echo "--- applying the patch to FlakyCodeChange/"
# Drop build output and binary entries: one inapplicable hunk aborts the whole patch.
SRCPATCH="$W/.patch_src.diff"
python3 - "$PATCH" "$SRCPATCH" <<'PYFILTER'
import re, sys, pathlib
raw = pathlib.Path(sys.argv[1]).read_text(errors="replace")
BUILD = {"target", "build", "out", "bin", ".git", ".mvn"}
kept = []
for part in re.split(r"(?m)^(?=diff --git )", raw):
    if not part.strip() or "GIT binary patch" in part or "Binary files" in part:
        continue
    m = re.search(r"(?m)^\+\+\+ (\S+)", part)
    if not m:
        continue
    rel = m.group(1)
    rel = rel[2:] if rel.startswith(("a/", "b/")) else rel
    if not rel or rel == "dev/null" or (BUILD & set(pathlib.Path(rel).parts)):
        continue
    kept.append(part)
pathlib.Path(sys.argv[2]).write_text("".join(kept) or raw)
PYFILTER
APPLY_OUT="$(cd "$W/FlakyCodeChange" && patch -p1 --no-backup-if-mismatch -i "$SRCPATCH" 2>&1)"
APPLY_RC=$?
printf '%s\n' "$APPLY_OUT" | sed 's/^/    /'
if (( APPLY_RC != 0 )); then
  echo
  echo "    PATCH DID NOT APPLY (rc=$APPLY_RC)."
  echo "    Skipping the AFTER runs -- they would just repeat the baseline and"
  echo "    read as 'not fixed', which is a different claim from 'not testable'."
  (( KEEP )) || docker rm -f "$CN" >/dev/null 2>&1
  exit 2
fi

echo
echo "--- AFTER the patch (all PASS = flakiness gone)"
run_victim AFTER

echo
echo "trees: $W          (FlakyCodeChange/ now has the patch applied)"
if (( KEEP )); then
  cat <<EOF
container '$CN' left running. To iterate:

  # edit files in your IDE under $W/FlakyCodeChange/ then re-run:
  docker exec $CN bash -c '
    cd /app/work/FlakyCodeChange &&
    mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS >/dev/null &&
    mvn surefire:test -pl $MODULE -Dtest="$VICTIM" $MVNOPTS 2>&1' | grep "Tests run:"

  # start over from the unpatched flaky tree:
  $0 $CONTAINER --reset --keep

  # finish:
  docker rm -f $CN
EOF
else
  docker rm -f "$CN" >/dev/null 2>&1
  echo "(container removed; pass --keep to iterate by hand)"
fi
