#!/usr/bin/env bash
# archive_orchestrator_run.sh - copy a finished legacy orchestrator run into
# the same durable layout used by the Claude CLI pipeline:
#
#     data/<container>/run_<NN>/
#
# Auto-increments run_<NN> and never overwrites a previous run. Copies the
# orchestrator outputs only, dropping stale Claude CLI driver artifacts that may
# share the claude_outputs directory.
#
# Usage: archive_orchestrator_run.sh <container> <steps_out_dir> <model> <reproflake_dir>
set -euo pipefail

CONTAINER="${1:?container}"
STEPS_OUT_DIR="${2:?steps_out_dir}"
MODEL="${3:-claude-sonnet-4-6}"
REPROFLAKE_DIR="${4:?reproflake_dir}"

if [[ ! -d "$STEPS_OUT_DIR" ]]; then
  echo "[archive] no claude_outputs at $STEPS_OUT_DIR - nothing to archive" >&2
  exit 0
fi

base="$REPROFLAKE_DIR/data/$CONTAINER"
mkdir -p "$base"

n=1
while :; do
  run_label="$(printf 'run_%02d' "$n")"
  [[ ! -e "$base/$run_label" ]] && break
  n=$((n + 1))
done

run_dir="$base/$run_label"
mkdir -p "$run_dir/claude_outputs"

# Copy orchestrator outputs only; skip stale Claude driver leftovers.
for f in "$STEPS_OUT_DIR"/*; do
  [[ -e "$f" ]] || continue
  case "$(basename "$f")" in
    trial.ndjson|thinking.txt|tool_calls.jsonl|prompt_system.txt|prompt_user.txt|claude.stderr|usage.json)
      continue ;;
  esac
  cp -pR "$f" "$run_dir/claude_outputs/"
done

# Mirror the key artifacts at the run root.
if [[ -f "$STEPS_OUT_DIR/patch.diff"      ]]; then cp -p "$STEPS_OUT_DIR/patch.diff"      "$run_dir/Fixed.patch"; fi
if [[ -f "$STEPS_OUT_DIR/run_verdict.txt" ]]; then cp -p "$STEPS_OUT_DIR/run_verdict.txt" "$run_dir/"; fi
if [[ -f "$STEPS_OUT_DIR/run_summary.csv" ]]; then cp -p "$STEPS_OUT_DIR/run_summary.csv" "$run_dir/"; fi

verdict="$(cat "$STEPS_OUT_DIR/run_verdict.txt" 2>/dev/null           || cat "$STEPS_OUT_DIR/verify_after_fix.verdict" 2>/dev/null           || echo UNKNOWN)"
printf '%s
' "$verdict" > "$run_dir/.run_complete"
printf '{"container":"%s","model":"%s","verdict":"%s"}
'   "$CONTAINER" "$MODEL" "$verdict" > "$run_dir/orchestrator_meta.json"

echo "[archive] orchestrator run -> ${run_dir#"$REPROFLAKE_DIR"/}  (verdict=$verdict)"
