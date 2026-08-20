#!/usr/bin/env python3
"""
run_agentic_pass_at_k.py — pass@k harness for the agentic pipeline.

Direct counterpart to TraceMop Scripts/run_pass_at_k.py, adapted for:
  - Per-type entry points under `agentic/` (run_agentic_<type>.sh)
  - Claude CLI models only.
  - Per-run archive layout that includes the agentic conversation
    transcript + per-iteration log produced by agentic_claude_cli.py
  - Reusing the existing CSV writer / pass@k metric so the agentic
    Complete Containers Summary stays joinable with non-agentic runs

Usage:
  ./run_agentic_pass_at_k.py <container> [--runs 3] [--max-iterations 10]
                             [--model claude-sonnet-4-6]
                             [--max-budget-usd 0.50] [--verify-pass-runs 10]
                             [--cli-timeout-s 2400] [--force-rebuild-image]

Run output layout:
  data/<container>/run_<NN>/
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import agentic_config  # type: ignore  # noqa: E402

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
DATA_DIR = REPROFLAKE_DIR / "data"
CSV_FILE = REPROFLAKE_DIR / "test_config.csv"

TYPE_TO_SCRIPT = {
    "od":  SCRIPT_DIR / "run_agentic_od.sh",
    "td":  SCRIPT_DIR / "run_agentic_td.sh",
    "id":  SCRIPT_DIR / "run_agentic_id.sh",
    "nio": SCRIPT_DIR / "run_agentic_nio.sh",
}

# Claude CLI mode only supports Claude model IDs.
def _api_key_var(model_id: str) -> str:
    key = (model_id or "").strip().lower()
    if not key.startswith("claude"):
        sys.exit(
            f"ERROR: Claude CLI mode supports only Claude models; got '{model_id}'."
        )
    return "ANTHROPIC_API_KEY"

SENTINEL = ".run_complete"

# Reuse the cross-invocation log alongside the non-agentic runs, but separate
# by an `agentic` column so the existing reader scripts can filter. We DO use
# the same file path so a single dashboard sees both pipelines side-by-side.
COMPLETE_SUMMARY_FILE = REPROFLAKE_DIR / "Complete_Containers_Summary.csv"
COMPLETE_SUMMARY_COLS = [
    "timestamp", "container", "test_type", "model", "run", "final verdict",
    "rv_traces_used",
    "input_tokens", "output_tokens", "total_tokens", "llm_seconds",
    "validation_requested", "validation_runs", "validation_valid",
    "validation_passes",
    "validation_failures", "validation_incomplete", "reason_code",
    "validation_reason_code", "evaluation_incomplete",
    "temperature", "tools_used",
]


# ---------------------------------------------------------------------------
# Pre-flight
# ---------------------------------------------------------------------------

def load_csv_row(container):
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("result_container") or "").strip() == container:
                return row
    return None


def preflight(container):
    if not CSV_FILE.is_file():
        sys.exit(f"ERROR: CSV not found: {CSV_FILE}")
    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in CSV")
    test_type = row["test_type"].strip().lower()
    if test_type not in TYPE_TO_SCRIPT:
        sys.exit(f"ERROR: unsupported test_type '{test_type}' "
                 f"(supported: {', '.join(sorted(TYPE_TO_SCRIPT))})")
    script = TYPE_TO_SCRIPT[test_type]
    if not script.is_file():
        sys.exit(f"ERROR: agentic per-type script not found: {script}")
    if subprocess.run(["docker", "info"], capture_output=True).returncode != 0:
        sys.exit("ERROR: Docker daemon not reachable")
    return row, test_type, script


def docker_image_for_java(java_version: str) -> str:
    return {
        "8": "flaky_base_jdk8",
        "11": "flaky_base_jdk11",
        "17": "flaky_base_jdk17",
    }.get(str(java_version).strip(), "flaky_base_jdk8")


def restore_workspace_owner(container_name: str, data_dir: Path | None = None,
                            image: str | None = None):
    """Return bind-mounted outputs created by Docker root to the host user."""
    if not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return
    uid, gid = os.getuid(), os.getgid()
    result = subprocess.run(
        ["docker", "exec", "-u", "0", container_name,
         "chown", "-R", f"{uid}:{gid}", "/app/work"],
        capture_output=True,
    )
    if result.returncode == 0 or not data_dir or not image or not data_dir.is_dir():
        return
    subprocess.run(
        ["docker", "run", "--rm",
         "--mount", f"type=bind,source={data_dir},target=/app/work",
         image, "chown", "-R", f"{uid}:{gid}", "/app/work"],
        capture_output=True,
    )


def cleanup_completed_source_dirs(per_run_dir: Path, verdict: str):
    """Drop large reconstructed source trees after PASSED or FAILED runs."""
    if verdict not in {"PASSED", "FAILED"}:
        return
    removed = []
    for name in ("Fixed", "FixedCodeChange", "Flaky", "Flaky.pristine",
                 "Flakym2", "FlakyCodeChange"):
        path = per_run_dir / name
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(name)
    if removed:
        print(f"[wrapper] cleaned completed-run source dirs: {', '.join(removed)}")


# ---------------------------------------------------------------------------
# Per-run parse
# ---------------------------------------------------------------------------

def safe_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


VALID_VERDICTS = {"PASSED", "FAILED", "INCOMPLETE"}
PUBLIC_VERDICTS = {"PASSED", "FAILED"}
INCOMPLETE_FAIL_CLOSED_REASON = "EVALUATION_INCOMPLETE_FAIL_CLOSED"


def _normalise_verdict(value):
    """Return the internal validation state represented by *value*.

    The TD validator's JSON uses reason-bearing states (for example
    ``infra_error`` and ``oracle_merge_conflict``). Reports retain this state for
    attempt accounting, but publish only PASSED/FAILED at the run level. Keep
    this conversion deliberately conservative so the caller can explicitly
    fail closed on unknown/incomplete evaluation state.
    """
    if value is None:
        return None
    value = str(value).strip().upper().replace("-", "_").replace(" ", "_")
    if value in VALID_VERDICTS:
        return value
    if value in {"PASS", "SUCCESS", "SUCCEEDED"}:
        return "PASSED"
    if value in {"FAIL", "TEST_FAILED", "TEST_FAILURE", "ASSERTION_FAILED"}:
        return "FAILED"
    if value in {
        "INFRA", "INFRA_ERROR", "INFRA_FAILURE", "UNSCORABLE",
        "ORACLE_MERGE_CONFLICT", "NON_DISCRIMINATIVE", "BLOCKED",
    }:
        return "INCOMPLETE"
    return None


def _public_verdict(internal_state):
    """Map evaluator state to the two public verdicts, failing closed."""
    return "PASSED" if internal_state == "PASSED" else "FAILED"


def _json_scopes(payload):
    """Yield the common nesting levels used by validation result JSON."""
    if not isinstance(payload, dict):
        return
    yield payload
    for key in ("result", "aggregate", "validation", "summary", "totals",
                "stats", "compile", "composition"):
        child = payload.get(key)
        if isinstance(child, dict):
            yield child


def _first_json_value(payload, keys):
    for scope in _json_scopes(payload) or ():
        for key in keys:
            value = scope.get(key)
            if value is not None and value != "":
                return value
    return None


def _int_json_value(payload, keys):
    value = _first_json_value(payload, keys)
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reason_from_json(payload):
    """Extract, without discarding, a validator reason code and detail."""
    raw = _first_json_value(payload, (
        "reason_code", "failure_reason_code", "category", "reason",
        "terminal_reason", "final_reason",
    ))
    detail = _first_json_value(payload, (
        "reason_detail", "reason_message", "message", "detail", "error",
    ))
    code = ""
    if isinstance(raw, dict):
        code = str(raw.get("code") or raw.get("category") or "").strip()
        if not detail:
            detail = raw.get("message") or raw.get("detail")
    elif raw is not None:
        code = str(raw).strip()
    if isinstance(detail, (dict, list)):
        detail = json.dumps(detail, sort_keys=True)
    return code, str(detail or "").strip()


def _attempt_records_from_json(payload):
    """Find embedded per-attempt result objects in an aggregate document."""
    for scope in _json_scopes(payload) or ():
        for key in ("attempts", "runs", "results", "validation_results"):
            value = scope.get(key)
            if isinstance(value, list):
                records = [item for item in value if isinstance(item, dict)]
                if records:
                    return records
            if isinstance(value, dict):
                records = [item for item in value.values()
                           if isinstance(item, dict)]
                if records and any(
                        _normalise_verdict(_first_json_value(item, (
                            "verdict", "run_verdict", "final_verdict",
                            "status", "outcome")))
                        for item in records):
                    return records
    return []


def _attempt_result_files(validation_dirs):
    """Return only validation-attempt results, excluding baseline/build JSON."""
    files = []
    for validation_dir in validation_dirs:
        for dirname in ("runs", "attempts"):
            root = validation_dir / dirname
            if root.is_dir():
                files.extend(root.glob("*/result.json"))
                # agentic_verify archives one selected result per validation run
                # as attempt_NN.json, alongside attempt_NN.command_MM.json.
                files.extend(root.glob("attempt_[0-9][0-9].json"))
    return sorted(set(files), key=lambda p: str(p))


def read_validation_evidence(per_run_dir: Path, meta: dict | None = None):
    """Read the new TD validation evidence without modifying any artifact.

    ``claude_outputs/td_validation`` is canonical.  The run-root location is
    also accepted so partially migrated runs remain reportable.  Per-attempt
    result files are preferred over inferred/configured counts; older runs fall
    back to the number of confirmations that were actually recorded in meta.
    """
    steps = per_run_dir / "claude_outputs"
    validation_dirs = []
    for path in (steps / "td_validation", per_run_dir / "td_validation"):
        if path.is_dir() and path not in validation_dirs:
            validation_dirs.append(path)

    aggregate = None
    aggregate_path = None
    for validation_dir in validation_dirs:
        for name in ("aggregate.json", "result.json"):
            candidate = validation_dir / name
            payload = safe_json(candidate)
            if isinstance(payload, dict):
                aggregate, aggregate_path = payload, candidate
                break
        if aggregate is not None:
            break

    result_files = _attempt_result_files(validation_dirs)
    file_records = []
    for path in result_files:
        payload = safe_json(path)
        if isinstance(payload, dict):
            file_records.append(payload)
    embedded_records = _attempt_records_from_json(aggregate)
    records = file_records or embedded_records

    verdict = _normalise_verdict(_first_json_value(aggregate, (
        "run_verdict", "verdict", "final_verdict", "outcome", "status")))
    # The aggregate is internal validator evidence and may legitimately retain
    # INCOMPLETE.  It is never emitted as the public run verdict; callers map
    # it to FAILED while keeping this flag and the original reason for audit.
    aggregate_internal = _normalise_verdict(
        _first_json_value(aggregate, ("internal_status", "validation_status")))
    terminal_ready = bool(aggregate) and aggregate.get("terminal_ready") is True
    evaluation_incomplete = bool(aggregate) and (
        verdict not in PUBLIC_VERDICTS
        or aggregate_internal == "INCOMPLETE"
        or bool(aggregate.get("evaluation_incomplete"))
        or not terminal_ready)
    reason_code, reason_detail = _reason_from_json(aggregate)
    for validation_dir in validation_dirs:
        auxiliary = safe_json(validation_dir / "orchestrator_result.json")
        if not isinstance(auxiliary, dict):
            continue
        auxiliary_state = _normalise_verdict(auxiliary.get("internal_status"))
        if auxiliary_state == "INCOMPLETE":
            evaluation_incomplete = True
        if not reason_code:
            auxiliary_code, auxiliary_detail = _reason_from_json(auxiliary)
            # Keep the low-level cause in diagnostic fields.  The public
            # reason_code is assigned by parse_run() when it fails closed.
            reason_code = str(
                auxiliary.get("diagnostic_reason_code") or auxiliary_code or ""
            ).strip()
            reason_detail = auxiliary_detail
        if reason_code or reason_detail:
            break
    if not reason_code:
        for record in reversed(records):
            record_verdict = _normalise_verdict(_first_json_value(record, (
                "verdict", "run_verdict", "final_verdict", "outcome", "status")))
            if record_verdict and record_verdict != "PASSED":
                reason_code, reason_detail = _reason_from_json(record)
                if reason_code or reason_detail:
                    break

    # The aggregate schema distinguishes requested from actual attempts. Honour
    # its explicit actual count; otherwise count concrete run/result records.
    # Never substitute requested_attempts or agentic_config.VERIFY_PASS_RUNS.
    explicit_attempts = _int_json_value(aggregate, (
        "actual_attempts", "completed_attempts", "attempt_count",
        "validation_attempts", "validation_runs", "runs_completed",
    ))
    attempts = explicit_attempts if explicit_attempts is not None else len(records)
    requested = _int_json_value(aggregate, ("requested_attempts",)) or 0
    valid = _int_json_value(aggregate, ("valid_attempts",))

    attempt_verdicts = [
        _normalise_verdict(_first_json_value(record, (
            "verdict", "run_verdict", "final_verdict", "outcome", "status")))
        for record in records
    ]
    passes = _int_json_value(aggregate, (
        "passed_attempts", "pass_count", "validation_passes"))
    failures = _int_json_value(aggregate, (
        "failed_attempts", "failure_count", "validation_failures"))
    incomplete = _int_json_value(aggregate, (
        "incomplete_attempts", "infra_attempts", "validation_incomplete"))
    if passes is None:
        passes = sum(v == "PASSED" for v in attempt_verdicts)
    if failures is None:
        failures = sum(v == "FAILED" for v in attempt_verdicts)
    if incomplete is None:
        incomplete = sum(v == "INCOMPLETE" for v in attempt_verdicts)
    if valid is None:
        valid = passes + failures

    archive_logs = []
    for validation_dir in validation_dirs:
        runs_dir = validation_dir / "runs"
        if runs_dir.is_dir():
            archive_logs.extend(runs_dir.glob("attempt_[0-9][0-9].log"))

    # Per-attempt archives exist only when agentic_verify.py is invoked with
    # --attempt, which agentic_claude_cli.py passes for td runs alone. Every
    # other type writes an aggregate but no archives, so requiring one per
    # attempt would compare 0 against N and fail every od/id/nio run closed.
    # Demand them when the writer promises them, or when a partial set is
    # present -- incomplete archives are still contradictory evidence.
    archives_expected = ((meta or {}).get("test_type") == "td"
                         or bool(result_files) or bool(archive_logs))
    # A PASS is a completeness claim, not merely a label.  Every requested
    # attempt must exist, be valid, and pass; contradictory or partial
    # aggregate evidence fails closed even if its top-level verdict says PASS.
    if verdict == "PASSED" and (
            requested < 1
            or attempts != requested
            or valid != attempts
            or passes != attempts
            or failures != 0
            or incomplete != 0
            or len(embedded_records) != attempts
            or (archives_expected and len(result_files) != attempts)
            or (archives_expected and len(set(archive_logs)) != attempts)
            or any(value != "PASSED" for value in attempt_verdicts)):
        evaluation_incomplete = True
    if (aggregate_internal in PUBLIC_VERDICTS
            and verdict in PUBLIC_VERDICTS
            and aggregate_internal != verdict):
        evaluation_incomplete = True

    stats = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0,
             "failure_markers": 0}
    stat_aliases = {
        "tests": ("tests", "tests_run"),
        "failures": ("failures", "test_failures"),
        "errors": ("errors", "test_errors"),
        "skipped": ("skipped", "test_skipped", "skips"),
        "failure_markers": ("failure_markers", "markers"),
    }
    if records:
        for record in records:
            stats_record = record
            command_attempts = record.get("attempts")
            if isinstance(command_attempts, list):
                selected = record.get("selected_command_attempt",
                                      record.get("selected_attempt"))
                matches = [item for item in command_attempts
                           if isinstance(item, dict) and
                           (selected is None or item.get("attempt") == selected)]
                if matches:
                    stats_record = matches[-1]
            for name, aliases in stat_aliases.items():
                stats[name] += _int_json_value(stats_record, aliases) or 0
    else:
        for name, aliases in stat_aliases.items():
            stats[name] = _int_json_value(aggregate, aliases) or 0

    # Legacy Claude metadata records only confirmations.  Count the initial run
    # only when its log exists, rather than reporting the configured target.
    if attempts == 0 and isinstance(meta, dict):
        confirmations = meta.get("confirm_runs")
        if isinstance(confirmations, list):
            attempts = len(confirmations)
            had_initial = (steps / "verify_after_fix.log").is_file()
            if had_initial:
                attempts += 1
            legacy_verdicts = [
                _normalise_verdict(item.get("verdict"))
                for item in confirmations if isinstance(item, dict)
            ]
            passes = sum(v == "PASSED" for v in legacy_verdicts)
            failures = sum(v == "FAILED" for v in legacy_verdicts)
            incomplete = sum(v == "INCOMPLETE" for v in legacy_verdicts)
            # Confirmation runs only exist after the initial verification passed.
            # For a one-run legacy PASS, meta's final verdict supplies that fact.
            if had_initial and (confirmations or
                                _normalise_verdict(meta.get("verdict")) == "PASSED"):
                passes += 1
            valid = passes + failures

    return {
        "aggregate": aggregate or {},
        "aggregate_path": str(aggregate_path.relative_to(per_run_dir))
        if aggregate_path else "",
        "terminal_ready": terminal_ready,
        "verdict": verdict,
        "reason_code": reason_code,
        "reason_detail": reason_detail,
        "evaluation_incomplete": evaluation_incomplete,
        "requested": requested,
        "attempts": attempts,
        "valid": valid or 0,
        "passes": passes,
        "failures": failures,
        "incomplete": incomplete,
        "stats": stats,
    }


def parse_run(per_run_dir: Path, container, test_type, run_n, model="claude"):
    """Extract a single CSV row's worth of data from an agentic per-run
    folder. Returns a dict shaped to match parse_run in the non-agentic
    harness so downstream summary writers don't need to branch.
    """
    steps = per_run_dir / "claude_outputs"
    meta = safe_json(per_run_dir / "claude_outputs" / "meta.json") or {}
    model = meta.get("model") or model
    run_verdict_file = steps / "run_verdict.txt"          # authoritative binary
    verdict_file = steps / "verify_after_fix.verdict"     # binary fallback
    apply_file = steps / "apply_report.json"
    llm_resp = steps / "llm_response.json"
    iter_log = steps / "agentic_iterations.jsonl"
    tool_calls_file = steps / "tool_calls.jsonl"
    verify_log = steps / "verify_after_fix.log"
    pipeline = per_run_dir / "pipeline.log"

    validation = read_validation_evidence(per_run_dir, meta)

    # Prefer the authoritative run verdict, then the structured validator
    # aggregate, and finally the binary verify file used by older runs.
    internal_verdict = None
    verdict_source = "default"
    if run_verdict_file.is_file():
        # File existence is authoritative.  An empty/unrecognised value is
        # malformed terminal evidence and therefore fails closed; do not fall
        # through to a potentially stale legacy PASS.
        internal_verdict = _normalise_verdict(
            run_verdict_file.read_text(encoding="utf-8", errors="replace"))
        verdict_source = "run_verdict.txt"
    if verdict_source == "default" and validation.get("verdict"):
        internal_verdict = validation["verdict"]
        verdict_source = validation.get("aggregate_path") or "td_validation"
    if verdict_source == "default":
        value = _normalise_verdict(meta.get("verdict"))
        if value:
            internal_verdict, verdict_source = value, "meta.json"
    if verdict_source == "default" and verdict_file.is_file():
        value = _normalise_verdict(
            verdict_file.read_text(encoding="utf-8", errors="replace"))
        if value:
            internal_verdict, verdict_source = value, "verify_after_fix.verdict"

    aggregate_internal = validation.get("verdict")
    evaluation_incomplete = bool(
        internal_verdict not in PUBLIC_VERDICTS
        or aggregate_internal == "INCOMPLETE"
        or validation.get("evaluation_incomplete")
        or (test_type == "td" and (
            not validation.get("aggregate")
            or not run_verdict_file.is_file()))
        or (internal_verdict in PUBLIC_VERDICTS
            and aggregate_internal in PUBLIC_VERDICTS
            and internal_verdict != aggregate_internal)
    )
    verdict = ("FAILED" if evaluation_incomplete
               else _public_verdict(internal_verdict))

    apply_rep = safe_json(apply_file) or {}
    resp = safe_json(llm_resp) or {}

    # Claude CLI writes usage.json as a wrapper object:
    # {"usage": {...token fields...}, "duration_ms": ...}. The old
    # orchestrator wrote token fields directly on llm_response.json["usage"].
    usage_blob = safe_json(steps / "usage.json") or {}
    meta_usage = meta.get("usage") or {}
    usage = (resp.get("usage") or usage_blob.get("usage") or
             meta_usage.get("usage") or usage_blob or meta_usage or {})
    in_tokens = ((usage.get("input_tokens") or 0)
                 + (usage.get("cache_creation_input_tokens") or 0)
                 + (usage.get("cache_read_input_tokens") or 0))
    out_tokens = usage.get("output_tokens") or 0
    total = in_tokens + out_tokens
    duration_ms = (resp.get("duration_ms") or usage_blob.get("duration_ms") or
                   meta_usage.get("duration_ms") or 0)
    elapsed_llm = float(resp.get("elapsed_seconds") or
                        ((duration_ms or 0) / 1000.0))

    # Read per-iteration jsonl tail for finer-grained data if needed.
    iterations = []
    if iter_log.is_file():
        for line in iter_log.read_text(encoding="utf-8",
                                       errors="replace").splitlines():
            try:
                iterations.append(json.loads(line))
            except Exception:
                continue

    # apply_report: layer + compile/recompile state.
    layers = apply_rep.get("layers_attempted") or []
    result = apply_rep.get("result") or {}
    apply_layer = result.get("layer") or "none"
    rc = apply_rep.get("recompile") or {}
    recompile_ok = rc.get("ok") if rc and not rc.get("skipped") else None
    compile_d = apply_rep.get("compile") or {}
    host_compile_ok = (compile_d.get("all_ok") if compile_d
                       and not compile_d.get("skipped") else None)
    path_rewritten = any(bool(la.get("path_rewritten")) for la in layers)
    imports_inferred = []
    for la in layers:
        for ap in (la.get("applied") or []):
            imports_inferred.extend(ap.get("imports_inferred") or [])

    # Structured attempt results are authoritative. Fall back to the legacy
    # canonical log only when the validator did not publish test statistics.
    validation_stats = validation.get("stats") or {}
    tests = int(validation_stats.get("tests") or 0)
    failures = int(validation_stats.get("failures") or 0)
    errors = int(validation_stats.get("errors") or 0)
    skipped = int(validation_stats.get("skipped") or 0)
    markers = int(validation_stats.get("failure_markers") or 0)
    fail_snippet = ""
    if not validation.get("aggregate") and verify_log.is_file():
        log = verify_log.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(
                r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*"
                r"Errors:\s*(\d+)(?:,\s*Skipped:\s*(\d+))?", log):
            tests, failures, errors = int(m.group(1)), int(m.group(2)), int(m.group(3))
            skipped = int(m.group(4) or 0)
        markers = len(re.findall(r"<<< (?:FAILURE|ERROR)!", log))
        if markers > 0:
            for line in log.splitlines():
                if "<<< FAILURE!" in line or "<<< ERROR!" in line:
                    fail_snippet = line.strip()[:200]
                    break

    elapsed_total = 0.0
    sentinel_file = per_run_dir / SENTINEL
    if sentinel_file.is_file():
        for line in sentinel_file.read_text(encoding="utf-8",
                                            errors="replace").splitlines():
            if line.startswith("elapsed="):
                try:
                    elapsed_total = float(line.split("=", 1)[1])
                except ValueError:
                    pass
                break

    validation_reason_code = validation.get("reason_code") or ""
    reason_code = (INCOMPLETE_FAIL_CLOSED_REASON if evaluation_incomplete
                   else validation_reason_code)
    reason_detail = validation.get("reason_detail") or ""
    cat = classify(verdict, apply_rep, recompile_ok, failures, errors,
                   markers, pipeline, reason_code=reason_code)
    if not fail_snippet and reason_detail:
        fail_snippet = reason_detail.replace("\n", " | ")[:200]
    if not fail_snippet and validation_reason_code and verdict != "PASSED":
        fail_snippet = validation_reason_code[:200]
    if not fail_snippet and reason_code and verdict != "PASSED":
        fail_snippet = reason_code[:200]
    if not fail_snippet and verdict != "PASSED":
        for la in layers:
            r = la.get("reason") or ""
            if r:
                fail_snippet = r.replace("\n", " | ")[:200]
                break
        if not fail_snippet:
            fail_snippet = result.get("reason", "")[:200]

    # Aggregate tool usage into a compact "name:count; name:count" string.
    # The old orchestrator wrote agentic_iterations.jsonl; Claude CLI writes
    # one tool-use record per line in tool_calls.jsonl.
    tool_counts = {}
    for it in iterations:
        for t in (it.get("tools_used") or []):
            tool_counts[t] = tool_counts.get(t, 0) + 1
    if tool_calls_file.is_file():
        for line in tool_calls_file.read_text(encoding="utf-8",
                                              errors="replace").splitlines():
            try:
                rec = json.loads(line)
            except Exception:
                continue
            name = rec.get("name")
            if name:
                tool_counts[name] = tool_counts.get(name, 0) + 1
    tools_used_str = "; ".join(
        f"{name}:{cnt}" for name, cnt in
        sorted(tool_counts.items(), key=lambda kv: (-kv[1], kv[0])))

    return {
        "container": container,
        "test_type": test_type,
        "model": model,
        "run": run_n,
        "verdict": verdict,
        "verdict_source": verdict_source,
        "reason_code": reason_code,
        "validation_reason_code": validation_reason_code,
        "evaluation_incomplete": evaluation_incomplete,
        "tools_used": tools_used_str,
        "fail_category": cat,
        "input_tokens_total": in_tokens,
        "output_tokens_total": out_tokens,
        "total_tokens": total,
        "llm_finish_reason": resp.get("stop_reason") or "",
        "elapsed_llm_seconds": elapsed_llm,
        "apply_layer": apply_layer,
        "apply_path_rewritten": path_rewritten,
        "apply_imports_inferred": ";".join(imports_inferred),
        "recompile_ok": recompile_ok,
        "host_compile_ok": host_compile_ok,
        "verify_tests": tests,
        "verify_failures": failures,
        "verify_errors": errors,
        "verify_skipped": skipped,
        "failure_markers": markers,
        "validation_requested": int(validation.get("requested") or 0),
        "validation_runs": int(validation.get("attempts") or 0),
        "validation_valid": int(validation.get("valid") or 0),
        "validation_passes": int(validation.get("passes") or 0),
        "validation_failures": int(validation.get("failures") or 0),
        "validation_incomplete": int(validation.get("incomplete") or 0),
        "validation_artifact": validation.get("aggregate_path") or "",
        "fail_snippet": fail_snippet,
        "elapsed_total_seconds": round(elapsed_total, 1),
        "agentic_iterations": len(iterations) or int(usage_blob.get("num_turns") or 0),
    }


def classify(verdict, apply_rep, recompile_ok, failures, errors, markers,
             pipeline, reason_code=""):
    if verdict == "PASSED":
        return "passed"
    if reason_code:
        return reason_code
    if pipeline.is_file():
        log = pipeline.read_text(encoding="utf-8", errors="replace")
        if any(s in log for s in [
            "ERROR: Flaky run had Failures=0",
            "ERROR: Flaky+wrapper passed unexpectedly",
            "ERROR: NonDex run produced 0 failures",
        ]):
            return "sanity_failed"
    result = (apply_rep or {}).get("result") or {}
    if not result.get("ok") and result.get("layer") in (None, "none"):
        return "patch_apply_failed"
    if recompile_ok is False:
        return "compile_failed"
    if failures + errors > 0 or markers > 0:
        return "test_failed"
    return "unknown_failure"


# ---------------------------------------------------------------------------
# pass@k
# ---------------------------------------------------------------------------

def pass_at_k(n, c, k):
    if c == 0:
        return 0.0
    if n - c < k:
        return 1.0
    return 1.0 - math.comb(n - c, k) / math.comb(n, k)


# ---------------------------------------------------------------------------
# Summary writers
# ---------------------------------------------------------------------------

CSV_COLS = [
    "container", "test_type", "model", "run", "verdict", "fail_category",
    "verdict_source", "reason_code", "validation_reason_code",
    "evaluation_incomplete",
    "agentic_iterations",
    "input_tokens_total", "output_tokens_total", "total_tokens",
    "llm_finish_reason", "elapsed_llm_seconds", "elapsed_total_seconds",
    "apply_layer", "apply_path_rewritten", "apply_imports_inferred",
    "recompile_ok", "host_compile_ok",
    "validation_requested", "validation_runs", "validation_valid",
    "validation_passes", "validation_failures",
    "validation_incomplete", "validation_artifact",
    "verify_tests", "verify_failures", "verify_errors", "verify_skipped",
    "failure_markers",
    "fail_snippet",
]


def collect_all_rows_on_disk(runs_root: Path, container: str,
                             test_type: str, model: str = "claude") -> list:
    """Scan flat run_NN directories under data/<container>."""
    rows = []
    if not runs_root.is_dir():
        return rows
    run_dirs = []
    for d in runs_root.iterdir():
        if not d.is_dir():
            continue
        if not (d / SENTINEL).is_file():
            continue
        m = re.match(r"run_(\d+)$", d.name)
        if m:
            run_dirs.append((int(m.group(1)), d))
    run_dirs.sort()
    for run_n, d in run_dirs:
        rows.append(parse_run(d, container, test_type, run_n, model=model))
    return rows


def next_run_number(runs_root: Path) -> int:
    highest = 0
    if runs_root.is_dir():
        for d in runs_root.iterdir():
            if not d.is_dir():
                continue
            m = re.match(r"run_(\d+)$", d.name)
            if m:
                highest = max(highest, int(m.group(1)))
    return highest + 1


def append_complete_summary(rows):
    """Append per-run rows to the shared Complete Containers Summary.csv.
    Tagged with rv_traces_used='agentic' so the agentic rows are visually
    and machine-distinguishable from the non-agentic pass@k batches. Preserve
    columns owned by other pipelines when the shared header evolves.
    """
    if not rows:
        return
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    new_row_dicts = []
    for r in rows:
        new_row_dicts.append({
            "timestamp": timestamp,
            "container": r["container"],
            "test_type": r["test_type"],
            "model": r["model"],
            "run": f"run_{int(r['run']):02d}",
            "final verdict": r["verdict"],
            "rv_traces_used": "agentic",
            "input_tokens": r["input_tokens_total"],
            "output_tokens": r["output_tokens_total"],
            "total_tokens": r["total_tokens"],
            "llm_seconds": round(r["elapsed_llm_seconds"], 1),
            "validation_requested": r.get("validation_requested", 0),
            "validation_runs": r.get("validation_runs", 0),
            "validation_valid": r.get("validation_valid", 0),
            "validation_passes": r.get("validation_passes", 0),
            "validation_failures": r.get("validation_failures", 0),
            "validation_incomplete": r.get("validation_incomplete", 0),
            "reason_code": r.get("reason_code", ""),
            "validation_reason_code": r.get("validation_reason_code", ""),
            "evaluation_incomplete": r.get("evaluation_incomplete", False),
            "temperature": agentic_config.TEMPERATURE,
            "tools_used": r.get("tools_used", ""),
        })

    existing_header = []
    if COMPLETE_SUMMARY_FILE.is_file() and COMPLETE_SUMMARY_FILE.stat().st_size > 0:
        with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
            try:
                existing_header = next(csv.reader(f))
            except StopIteration:
                existing_header = []

    if existing_header and all(col in existing_header
                               for col in COMPLETE_SUMMARY_COLS):
        # An existing superset may contain non-agentic fields. Reuse it exactly.
        with open(COMPLETE_SUMMARY_FILE, "a", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=existing_header,
                                    quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writerows(new_row_dicts)
    else:
        existing_rows = []
        if existing_header:
            with open(COMPLETE_SUMMARY_FILE, encoding="utf-8", newline="") as f:
                existing_rows = list(csv.DictReader(f))
        for existing in existing_rows:
            if "verdict" in existing and not existing.get("final verdict"):
                existing["final verdict"] = existing.get("verdict", "")
        fields = list(existing_header)
        for col in COMPLETE_SUMMARY_COLS:
            if col not in fields:
                fields.append(col)
        if not fields:
            fields = list(COMPLETE_SUMMARY_COLS)
        tmp = COMPLETE_SUMMARY_FILE.with_suffix(
            COMPLETE_SUMMARY_FILE.suffix + ".tmp")
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fields,
                                    quoting=csv.QUOTE_ALL, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(existing_rows)
            writer.writerows(new_row_dicts)
        tmp.replace(COMPLETE_SUMMARY_FILE)
    print(f"[wrapper] appended {len(rows)} row(s) to "
          f"{COMPLETE_SUMMARY_FILE.name}")


def write_summary(rows, runs_root: Path, container, row_meta, runs_per_model,
                  log_prefix="[wrapper]"):
    csv_path = runs_root / "summary.csv"
    with open(csv_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_COLS, quoting=csv.QUOTE_ALL,
                           extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"{log_prefix} summary written: {csv_path.name}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--max-iterations", type=int, default=10,
                    help="Claude Code max turns per run (default 10)")
    ap.add_argument("--model", default="claude-sonnet-4-6",
                    help="Claude model id passed to agentic_claude_cli.py")
    ap.add_argument("--max-budget-usd", default=None,
                    help="hard Claude Code spend cap per run")
    ap.add_argument("--verify-pass-runs", type=int, default=None,
                    help="extra passing verification runs required after the first pass")
    ap.add_argument("--cli-timeout-s", type=int, default=None,
                    help="wall-clock cap in seconds for Claude Code")
    ap.add_argument("--force-rebuild-image", action="store_true",
                    help="rebuild the Docker image even if one already exists")
    args = ap.parse_args()

    # Optional per-run knobs, forwarded verbatim to the per-type shell script.
    script_flags = []
    if args.max_budget_usd is not None:
        script_flags += ["--max-budget-usd", str(args.max_budget_usd)]
    if args.verify_pass_runs is not None:
        script_flags += ["--verify-pass-runs", str(args.verify_pass_runs)]
    if args.cli_timeout_s is not None:
        script_flags += ["--cli-timeout-s", str(args.cli_timeout_s)]
    if args.force_rebuild_image:
        script_flags.append("--force-rebuild-image")

    row, test_type, script = preflight(args.container)

    api_key_var = _api_key_var(args.model)
    api_key = (os.environ.get(api_key_var) or
               getattr(agentic_config, "ANTHROPIC_API_KEY", "") or "").strip()
    if not api_key:
        sys.exit(f"ERROR: {api_key_var} env var not set and no key found in config "
                 f"(required for model '{args.model}')")
    os.environ[api_key_var] = api_key

    runs_root = DATA_DIR / args.container
    runs_root.mkdir(parents=True, exist_ok=True)
    print(f"[wrapper] container={args.container}  test_type={test_type}  "
          f"runs={args.runs}  max_turns={args.max_iterations}  "
          f"model={args.model}")
    print(f"[wrapper] runs_root={runs_root}")

    container_name = "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container)
    docker_image = docker_image_for_java(row.get("java", "8"))

    rows = []
    start_run = next_run_number(runs_root)
    for run_n in range(start_run, start_run + args.runs):
        run_label = f"run_{run_n:02d}"
        per_run_dir = runs_root / run_label
        data_container_dir = per_run_dir
        sentinel = per_run_dir / SENTINEL

        per_run_dir.mkdir(parents=True, exist_ok=False)

        restore_workspace_owner(container_name, data_container_dir, docker_image)

        # Wipe dynamic outputs so this run can't be contaminated by stale
        # artefacts from the previous run (same rationale as the non-agentic
        # harness — see run_pass_at_k.py).
        for stale in ("claude_inputs", "claude_outputs", "result",
                      "traces-fixed", "traces-flaky", "traces-flakycc",
                      "traces-pass", "traces-fail"):
            stale_path = data_container_dir / stale
            if stale_path.is_dir():
                shutil.rmtree(stale_path, ignore_errors=True)

        print(f"[wrapper] === starting {args.model}/{run_label} ===")
        t0 = time.time()
        pipeline_log = per_run_dir / "pipeline.log"
        env = os.environ.copy()
        env["KEEP_CONTAINER"] = "1"
        env["AGENTIC_MAX_ITERATIONS"] = str(args.max_iterations)
        env["AGENTIC_MODEL"] = args.model
        env["AGENTIC_DRIVER"] = "claude_cli"
        env["AGENTIC_RUN_LABEL"] = run_label
        # Stream the Claude CLI driver's stdout live instead of block-buffering it
        # through this pipe, so [apply]/[verify] lines appear in real time.
        env["PYTHONUNBUFFERED"] = "1"
        env["AGENTIC_PYTHON"] = sys.executable

        with open(pipeline_log, "w", encoding="utf-8") as logf:
            p = subprocess.Popen(
                [str(script), args.container, *script_flags],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                env=env, text=True, bufsize=1,
            )
            for line in p.stdout:
                sys.stdout.write(line)
                logf.write(line)
            p.wait()
            exit_code = p.returncode

        restore_workspace_owner(container_name, data_container_dir, docker_image)

        elapsed = time.time() - t0
        print(f"[wrapper] === finished {args.model}/{run_label} "
              f"(exit={exit_code}, wall={elapsed:.0f}s) ===")

        # Normalise the public terminal artifact to the binary contract.  The
        # validator may retain INCOMPLETE internally, but reporting fails that
        # state closed and preserves its diagnostic evidence separately.
        steps_dir = per_run_dir / "claude_outputs"
        steps_dir.mkdir(parents=True, exist_ok=True)
        run_verdict_file = steps_dir / "run_verdict.txt"
        legacy_verdict_file = steps_dir / "verify_after_fix.verdict"
        meta_payload = safe_json(steps_dir / "meta.json") or {}
        validation_evidence = read_validation_evidence(per_run_dir, meta_payload)
        raw_run_verdict = (run_verdict_file.read_text(
            encoding="utf-8", errors="replace")
            if run_verdict_file.is_file() else None)
        run_state = _normalise_verdict(raw_run_verdict)
        aggregate_state = validation_evidence.get("verdict")
        meta_state = _normalise_verdict(meta_payload.get("verdict"))
        legacy_state = (
            _normalise_verdict(legacy_verdict_file.read_text(
                encoding="utf-8", errors="replace"))
            if legacy_verdict_file.is_file() else None
        )

        if raw_run_verdict is not None:
            internal_terminal = run_state
            terminal_source = "run_verdict.txt"
        elif aggregate_state:
            internal_terminal = aggregate_state
            terminal_source = (validation_evidence.get("aggregate_path")
                               or "td_validation")
        elif meta_state:
            internal_terminal = meta_state
            terminal_source = "meta.json"
        elif legacy_state:
            internal_terminal = legacy_state
            terminal_source = "verify_after_fix.verdict"
        else:
            internal_terminal = None
            terminal_source = "missing"

        evaluation_incomplete = bool(
            internal_terminal not in PUBLIC_VERDICTS
            or aggregate_state == "INCOMPLETE"
            or validation_evidence.get("evaluation_incomplete")
            or (test_type == "td" and (
                not validation_evidence.get("aggregate")
                or raw_run_verdict is None))
            or (internal_terminal in PUBLIC_VERDICTS
                and aggregate_state in PUBLIC_VERDICTS
                and internal_terminal != aggregate_state)
            or (internal_terminal == "PASSED" and exit_code != 0)
        )
        public_terminal = ("FAILED" if evaluation_incomplete
                           else _public_verdict(internal_terminal))

        if evaluation_incomplete:
            if raw_run_verdict is not None and run_state is None:
                diagnostic_reason = "invalid_run_verdict"
            elif validation_evidence.get("reason_code"):
                diagnostic_reason = validation_evidence["reason_code"]
            elif aggregate_state == "INCOMPLETE":
                diagnostic_reason = "aggregate_incomplete"
            elif internal_terminal == "INCOMPLETE":
                diagnostic_reason = "terminal_incomplete"
            else:
                diagnostic_reason = ("orchestrator_nonzero_exit"
                                     if exit_code != 0
                                     else "missing_terminal_verdict")
            validation_dir = steps_dir / "td_validation"
            validation_dir.mkdir(parents=True, exist_ok=True)
            orchestrator_result = validation_dir / "orchestrator_result.json"
            if not orchestrator_result.exists():
                with open(orchestrator_result, "x", encoding="utf-8") as f:
                    json.dump({
                        "schema_version": 1,
                        "verdict": "FAILED",
                        "reason_code": INCOMPLETE_FAIL_CLOSED_REASON,
                        "internal_status": "INCOMPLETE",
                        "diagnostic_reason_code": diagnostic_reason,
                        "terminal_source": terminal_source,
                        "exit_code": exit_code,
                        "requested_attempts": int(
                            validation_evidence.get("requested") or 0),
                        "actual_attempts": int(
                            validation_evidence.get("attempts") or 0),
                        "valid_attempts": int(
                            validation_evidence.get("valid") or 0),
                        "passed_attempts": int(
                            validation_evidence.get("passes") or 0),
                        "failed_attempts": int(
                            validation_evidence.get("failures") or 0),
                        "incomplete_attempts": int(
                            validation_evidence.get("incomplete") or 0),
                    }, f, indent=2)
                    f.write("\n")

        # Preserve a malformed/three-state/conflicting original before
        # replacing it with the canonical public value.
        canonical_raw = (raw_run_verdict or "").strip()
        if raw_run_verdict is not None and canonical_raw != public_terminal:
            validation_dir = steps_dir / "td_validation"
            validation_dir.mkdir(parents=True, exist_ok=True)
            internal_copy = validation_dir / "run_verdict.internal.txt"
            if not internal_copy.exists():
                internal_copy.write_text(raw_run_verdict, encoding="utf-8")
        run_verdict_file.write_text(f"{public_terminal}\n", encoding="utf-8")

        sentinel.write_text(f"exit_code={exit_code}\nelapsed={elapsed:.1f}\n")

        row_data = parse_run(per_run_dir, args.container, test_type, run_n,
                             model=args.model)
        row_data["elapsed_total_seconds"] = round(elapsed, 1)
        cleanup_completed_source_dirs(per_run_dir, row_data["verdict"])
        rows.append(row_data)

        all_rows = collect_all_rows_on_disk(runs_root, args.container,
                                            test_type, args.model)
        write_summary(all_rows, runs_root, args.container, row, args.runs)
        append_complete_summary([row_data])

    all_rows = collect_all_rows_on_disk(runs_root, args.container, test_type, args.model)
    if all_rows:
        write_summary(all_rows, runs_root, args.container, row, args.runs)

    restore_workspace_owner(container_name, runs_root, docker_image)
    subprocess.run(["docker", "rm", "-f", container_name], capture_output=True)

    n = sum(1 for r in rows if r['verdict'] in ('PASSED', 'FAILED'))
    c = sum(1 for r in rows if r['verdict'] == 'PASSED')
    p1 = pass_at_k(n, c, 1) if n else 0.0
    pN = pass_at_k(n, c, n) if n else 0.0
    print(f"[wrapper] DONE. {c}/{n} runs PASSED  "
          f"pass@1={p1:.0%}  pass@{n}={pN:.0%}")

    # Exit nonzero when no run passed, so the dispatcher (run_agentic.py) and
    # any CI caller reflect the real repair outcome rather than just "the
    # batch completed". c = number of PASSED runs.
    sys.exit(0 if c > 0 else 1)


if __name__ == "__main__":
    main()
