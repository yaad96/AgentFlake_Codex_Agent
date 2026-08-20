#!/usr/bin/env bash
# ============================================================
# run_td_credible.sh — do the 22 credible LLM patches remove the flakiness?
#
#   ./run_td_credible.sh [container ...]
#
# For each container, with NO API cost:
#   1. stage into data_td_credible/<container>/
#   2. build FlakyCodeChange/ (pristine + timing perturbation) and run the
#      victim  -> BEFORE, must FAIL
#   3. apply td_credible_patches/<container>.diff
#   4. run the victim again -> AFTER, PASS means the flake is gone
#
# Results append to data_td_credible/results.jsonl. Resumable: a container
# already in results.jsonl is skipped. Source trees are deleted after each
# container to bound disk; the dataset zip is kept as a download cache.
#
# Env:
#   REPEAT=n      runs per side (default 3; these are timing tests)
#   KEEP_TREES=1  keep the staged trees for inspection
# ============================================================
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CSV="$ROOT/test_config.csv"
PATCHES="$SCRIPT_DIR/td_credible_patches"
OUT="$ROOT/data_td_credible"
RESULTS="$OUT/results.jsonl"
LOGS="$OUT/logs"
REPEAT="${REPEAT:-3}"
MVNOPTS='-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip -Denforcer.skip -Dmaven.javadoc.skip'
MIN_FREE_GB="${MIN_FREE_GB:-5}"

mkdir -p "$OUT" "$LOGS"; touch "$RESULTS"

if (( $# )); then TARGETS=("$@"); else
  TARGETS=(); for f in "$PATCHES"/*.diff; do TARGETS+=("$(basename "$f" .diff)"); done
fi

free_gb() { df -Pk "$OUT" | awk 'NR==2 {printf "%d", $4/1048576}'; }

# Docker runs as real root on Linux, so staged target/ dirs end up root-owned and
# a plain `rm -rf` fails -- and not every user has sudo. Hand ownership back using
# a throwaway container, which needs no host privileges at all.
reclaim() {
  [[ -d "$1" ]] || return 0
  local uid gid; uid="$(id -u)"; gid="$(id -g)"
  for img in flaky_base_jdk8 flaky_base_jdk8_hadoop alpine busybox; do
    if docker image inspect "$img" >/dev/null 2>&1; then
      docker run --rm -v "$1:/reclaim" "$img" chown -R "$uid:$gid" /reclaim >/dev/null 2>&1 && return 0
    fi
  done
  return 0
}

classify() {  # stdin = maven output
  awk '
    /Tests run: [0-9]+, Failures: [0-9]+, Errors: [0-9]+/ {
      match($0, /Tests run: [0-9]+/);  t=substr($0,RSTART+11,RLENGTH-11)+0
      match($0, /Failures: [0-9]+/);   f=substr($0,RSTART+10,RLENGTH-10)+0
      match($0, /Errors: [0-9]+/);     e=substr($0,RSTART+8,RLENGTH-8)+0
      T+=t; B+=f+e; seen=1
    }
    /COMPILATION ERROR/ { ce=1 }
    /===AF_BUILD_FAILED===|Could not find the selected project/ { bf=1 }
    END {
      if (!seen) { print (bf ? "BUILD_FAILED" : (ce ? "COMPILE_ERROR" : "NO_TESTS")); exit }
      if (T==0)  { print "NO_TESTS"; exit }
      print (B>0 ? "FAIL" : "PASS")
    }'
}

echo "run_td_credible: ${#TARGETS[@]} container(s), REPEAT=$REPEAT"
for C in "${TARGETS[@]}"; do
  grep -q "\"container\": \"$C\"" "$RESULTS" 2>/dev/null && { echo "[skip] $C"; continue; }
  (( $(free_gb) < MIN_FREE_GB )) && { echo "[halt] $(free_gb)G free"; break; }

  ROW=$(awk -F',' -v c="$C" '$1=="td" && $2==c {print; exit}' "$CSV")
  [[ -n "$ROW" ]] || { echo "[skip] $C (not a td row)"; continue; }
  IFS=',' read -r _ _ ZIP MODULE _ VICTIM _ _ JAVA _ URL <<< "$ROW"
  MODULE=${MODULE:-.}
  PATCH="$PATCHES/$C.diff"
  W="$OUT/$C"

  echo "[run ] $C  ($(date '+%H:%M:%S'), $(free_gb)G free)"
  reclaim "$W"; rm -rf "$W"; mkdir -p "$W"
  if [[ ! -f "$ROOT/data/$ZIP.zip" ]]; then
    curl -fsSL --retry 2 "$URL" -o "$ROOT/data/$ZIP.zip" || { echo "  download failed"; continue; }
  fi
  unzip -oq "$ROOT/data/$ZIP.zip" -d "$W"
  [[ -d "$W/$ZIP" ]] && { mv "$W/$ZIP"/* "$W/" 2>/dev/null; rmdir "$W/$ZIP" 2>/dev/null; }
  cp -r "$W/Flaky" "$W/FlakyCodeChange"
  patch -p1 -d "$W/FlakyCodeChange" < "$W/FlakyCodeChange.patch" >/dev/null 2>&1

  IMG=flaky_base_jdk8
  [[ "$JAVA" == 11 ]] && IMG=flaky_base_jdk11
  [[ "$JAVA" == 17 ]] && IMG=flaky_base_jdk17
  [[ "$(printf '%s' "$MODULE" | tr 'A-Z' 'a-z')" == *hadoop* ]] && IMG=flaky_base_jdk8_hadoop
  CN="tdc_$(printf '%s' "$C" | tr -c 'a-zA-Z0-9' '_')"
  docker rm -f "$CN" >/dev/null 2>&1
  mkdir -p "$W/Flakym2/.m2"
  PLAT=(); [[ "$(uname -m)" == "arm64" ]] && PLAT=(--platform linux/amd64)
  docker run -d "${PLAT[@]}" --name "$CN" \
    --mount "type=bind,source=$W,target=/app/work" \
    --mount "type=bind,source=$W/Flakym2/.m2,target=/root/.m2" \
    "$IMG" tail -f /dev/null >/dev/null || { echo "  docker run failed ($IMG)"; continue; }

  run_side() {  # $1 = label
    local i out v; local -a seen=()
    for (( i=1; i<=REPEAT; i++ )); do
      out=$(docker exec "$CN" bash -c "
        cd /app/work/FlakyCodeChange
        if ! mvn install -DskipTests -pl $MODULE -am -q $MVNOPTS 2>&1; then
          echo '===AF_BUILD_FAILED==='
        fi
        mvn surefire:test -pl $MODULE -Dtest='$VICTIM' $MVNOPTS 2>&1" 2>&1)
      printf '%s\n' "$out" > "$LOGS/$C.$1.$i.log"
      v=$(printf '%s\n' "$out" | classify)
      seen+=("$v"); echo "    $1 $i: $v" >&2
    done
    printf '%s\n' "${seen[@]}" | sort | uniq -c | awk '{printf "%s:%s ", $2, $1}'
  }

  BEFORE=$(run_side before)

  # Reduce the patch to its SOURCE hunks before applying. Captured patches carry
  # build output and binary entries (OOZIE-3683 has 4 binary hunks under
  # core/build/), and a single inapplicable hunk aborts the whole patch.
  SRCPATCH="$W/.patch_src.diff"
  python3 - "$PATCH" "$SRCPATCH" <<'PYFILTER'
import re, sys, pathlib
raw = pathlib.Path(sys.argv[1]).read_text(errors="replace")
BUILD = {"target", "build", "out", "bin", ".git", ".mvn"}
kept = []
for part in re.split(r"(?m)^(?=diff --git )", raw):
    if not part.strip():
        continue
    if "GIT binary patch" in part or "Binary files" in part:
        continue
    m = re.search(r"(?m)^\+\+\+ (\S+)", part)
    if not m:
        continue
    rel = m.group(1)
    rel = rel[2:] if rel.startswith(("a/", "b/")) else rel
    if not rel or rel == "dev/null":
        continue
    if BUILD & set(pathlib.Path(rel).parts):
        continue
    kept.append(part)
pathlib.Path(sys.argv[2]).write_text("".join(kept))
PYFILTER

  patch -p1 -d "$W/FlakyCodeChange" --no-backup-if-mismatch < "$SRCPATCH" > "$LOGS/$C.apply.log" 2>&1
  APPLIED=$?
  if (( APPLIED == 0 )); then
    AFTER=$(run_side after)
  else
    # The after runs would just repeat the baseline and read as "not fixed",
    # which is a different claim from "the patch could not be tested".
    echo "    patch did NOT apply -- skipping the after runs (see $LOGS/$C.apply.log)"
    AFTER=""
  fi

  python3 - "$C" "$RESULTS" "$APPLIED" "$BEFORE" "$AFTER" "$REPEAT" <<'PY'
import json, sys
c, res, applied, before, after, rep = sys.argv[1:7]
def d(s):
    out={}
    for tok in s.split():
        if ":" not in tok: continue
        k,v = tok.rsplit(":",1)
        if k in ("PASS","FAIL","NO_TESTS","COMPILE_ERROR") and v.isdigit():
            out[k]=int(v)
    return out
b,a=d(before),d(after)
rec={"container":c,"patch_applied":applied=="0","repeat":int(rep),
     "before":b,"after":a,
     "before_all_fail": b.get("FAIL",0)==int(rep),
     "after_all_pass":  a.get("PASS",0)==int(rep)}
rec["fixed"] = rec["before_all_fail"] and rec["after_all_pass"]
rec["outcome"] = ("FIXED" if rec["fixed"]
                  else "PATCH_DID_NOT_APPLY" if not rec["patch_applied"]
                  else "BASELINE_NOT_FAILING" if not rec["before_all_fail"]
                  else "NOT_FIXED")
open(res,"a").write(json.dumps(rec)+"\n")
print(f"    => before={before.strip()} after={after.strip() or '(not run)'} "
      f"outcome={rec['outcome']}")
PY

  # Docker runs as real root on Linux, so target/ ends up root-owned and the
  # host rm fails. Hand ownership back from inside the container first.
  docker exec "$CN" bash -c "chown -R $(id -u):$(id -g) /app/work" >/dev/null 2>&1
  docker rm -f "$CN" >/dev/null 2>&1
  [[ "${KEEP_TREES:-0}" == "1" ]] || reclaim "$W"
  [[ "${KEEP_TREES:-0}" == "1" ]] || rm -rf "$W/Flaky" "$W/FlakyCodeChange" "$W/Fixed" \
       "$W/FixedCodeChange" "$W/Flakym2"
done

echo
echo "=== summary ==="
python3 - "$RESULTS" <<'PY'
import json, sys
rows=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
from collections import Counter
for k, v in Counter(r.get("outcome", "?") for r in rows).most_common():
    print(f"  {v:3}  {k}")
print(f"  {len(rows):3}  TOTAL")
for r in rows:
    print(f"    {r['container'][:50]:52} before={r['before']} after={r['after']} "
          f"{r.get('outcome','')}")
PY
