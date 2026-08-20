#!/usr/bin/env python3
"""
agentic_verify.py

Type-aware verification helper for the agentic pipeline. Runs the appropriate
post-patch surefire (or NonDex) command inside the running docker container,
captures stdout/stderr, parses fresh Surefire XML when available (with a strict
text fallback), and writes:

    data/<container>/run_<NN>/claude_outputs/verify_after_fix.log
    data/<container>/run_<NN>/claude_outputs/verify_after_fix.verdict
    data/<container>/run_<NN>/claude_outputs/verify_after_fix.result.json
    data/<container>/run_<NN>/claude_outputs/verify_after_fix.attempt_NN.json

Public verdicts are exactly PASSED or FAILED. PASSED is intentionally strict:
the command must finish successfully, Maven must report BUILD SUCCESS, at least
one test must execute, and Failures/Errors/Skipped/markers must all be zero.
Internally, infrastructure failures, timeouts, malformed/missing reports, zero
tests, and skipped tests are tracked as INCOMPLETE, then exposed as FAILED with
an explicit ``*_FAIL_CLOSED`` reason. The process exits 0 only on PASSED and 1
otherwise, preserving the existing command-line contract.

Usage:
    python3 agentic_verify.py <result_container> [--docker-container NAME]

Requires:
    - The container `tm_<sanitized>` to already be running with the data dir
      bind-mounted.
    - For NIO: WRAPPER_FQCN env var (set by run_agentic_nio.sh; equal to the
      auto-generated wrapper class's FQN).
    - For ID:  NONDEXSEED and NONDEX_RUNS env vars (set by run_agentic_id.sh).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Dict, Iterable, List, Optional, Tuple

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
sys.path.insert(0, str(LLM_SCRIPTS_DIR))
from assemble_llm_context import DATA_DIR, load_csv_row  # type: ignore  # noqa: E402


def container_run_dir(container: str) -> Path:
    run_label = (os.environ.get("AGENTIC_RUN_LABEL") or "run_01").strip()
    return Path(DATA_DIR) / container / run_label

# Surefire summary line emitted at the end of every test invocation. Skipped is
# mandatory here: if an old/custom provider omits it and no fresh XML report is
# available, there is not enough evidence for PASSED.
SUMMARY_RE = re.compile(
    r"Tests run:\s*(\d+),\s*Failures:\s*(\d+),\s*Errors:\s*(\d+),"
    r"\s*Skipped:\s*(\d+)")
MARKER_RE = re.compile(r"<<< (?:FAILURE|ERROR)!")
BUILD_SUCCESS = "BUILD SUCCESS"
BUILD_FAILURE = "BUILD FAILURE"

PASSED = "PASSED"
FAILED = "FAILED"
INCOMPLETE = "INCOMPLETE"


def _public_verdict(validation_status: str) -> str:
    """Collapse internal tri-state validation to the two public verdicts."""
    return PASSED if validation_status == PASSED else FAILED


def _public_reason(validation_status: str, validation_reason: str) -> str:
    """Make inconclusive evidence visibly fail closed in public artifacts."""
    if validation_status == INCOMPLETE:
        return f"{validation_reason.upper()}_FAIL_CLOSED"
    return validation_reason.upper()

# Per-type Maven option set. Kept aligned with the shell scripts so test
# behaviour matches the non-agentic pipeline byte-for-byte where possible.
MVNOPTS_OD = ('-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip '
              '-Drat.skip -Denforcer.skip -Dmaven.javadoc.skip')
MVNOPTS_TD = MVNOPTS_OD
MVNOPTS_ID = (
    '-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false '
    '-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip '
    '-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip '
    '-Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip '
    '-Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip '
    '-Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true '
    '-Dossindex.skip=true -Dmaven.bundle.plugin.skip=true '
    '-Dmaven.parallel.force=false')
# NIO MVNOPTS adds the additional skips that the NIO shell script uses; the
# extra flags are no-ops on projects that don't define the relevant plugins.
MVNOPTS_NIO = MVNOPTS_ID + ' -Dfindbugs.skip=true'


@dataclass
class CommandResult:
    """Observable outcome of one Docker/Maven attempt."""

    returncode: Optional[int]
    timed_out: bool
    output: str
    duration_seconds: float
    error: str = ""


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _verify_wall_timeout() -> int:
    raw = (os.environ.get("AGENTIC_VERIFY_WALL_TIMEOUT_S") or "1800").strip()
    try:
        value = int(raw)
    except ValueError:
        value = 1800
    return max(1, value)


def _run_in_container(docker_container: str, command: str,
                      timeout_s: Optional[int] = None) -> CommandResult:
    """Execute one verifier command and preserve rc, timeout, and output."""
    # Prepend the JDK Maven already uses to PATH so any build plugin that
    # spawns a bare `java`/tool during the verify build (codegen, antrun,
    # JavaCC/jute, protoc) can find it. Guarded: no-op if JAVA_HOME is unset,
    # and only prepends the in-use JDK — cannot break a subject that already
    # had java on PATH. Mirrors the same hardening in apply_fix recompile.
    command = 'export PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$PATH"\n' + command
    wall_timeout = timeout_s if timeout_s is not None else _verify_wall_timeout()
    kill_grace = 15
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["docker", "exec", docker_container,
             "timeout", "-k", f"{kill_grace}s", f"{wall_timeout}s",
             "bash", "-c", command],
            capture_output=True, text=True,
            # The in-container timeout owns Maven and kills its full process
            # group. This longer host timeout is only a backstop for a wedged
            # Docker client, avoiding an orphan Maven process that can mutate
            # reports during the next validation attempt.
            timeout=wall_timeout + kill_grace + 15,
        )
        timed_out = proc.returncode in {124, 137}
        return CommandResult(
            returncode=proc.returncode,
            timed_out=timed_out,
            output=(proc.stdout or "") + (proc.stderr or ""),
            duration_seconds=round(time.monotonic() - started, 3),
            error=(f"verification exceeded {wall_timeout}s"
                   if timed_out else ""),
        )
    except subprocess.TimeoutExpired as exc:
        output = _text(exc.stdout) + _text(exc.stderr)
        return CommandResult(
            returncode=None,
            timed_out=True,
            output=output,
            duration_seconds=round(time.monotonic() - started, 3),
            error=f"verification exceeded {wall_timeout}s; docker exec wedged",
        )
    except OSError as exc:
        return CommandResult(
            returncode=None,
            timed_out=False,
            output="",
            duration_seconds=round(time.monotonic() - started, 3),
            error=f"{type(exc).__name__}: {exc}",
        )


MISSING_PROPS_GOAL = "Could not find goal 'properties' in plugin"


def _build_command(test_type: str, row: dict, no_props: bool = False,
                   container_workdir: str = "/app/work/Flaky") -> str:
    """Construct the in-container `mvn ... ` command for the given test
    type. The shell scripts' verify_victim() functions are the canonical
    source — keep this in lock-step with them.
    """
    module = (row.get("module") or ".").strip()
    victim = (row.get("flaky_test") or "").strip()
    polluter = (row.get("polluter/state setter") or "").strip()
    # Never let a missing oracle tree fall through to Docker's default cwd: a
    # successful build there would validate the wrong source tree.
    cd = f"cd {shlex.quote(container_workdir)} || exit 97\n"
    # `dependency:properties` resolves the argLine placeholders some poms use
    # (see the OD/TD notes below), but the goal only exists in
    # maven-dependency-plugin >= 2.2. Projects that PIN an older one -- Apex
    # pins 2.1 -- die instantly with
    #   Could not find goal 'properties' in plugin ...:maven-dependency-plugin:2.1
    # i.e. BUILD FAILURE in <1s, Tests run: 0, and the repair is scored FAILED
    # without a test ever running. main() retries with props_goal="" on that
    # error, so both project families work.
    props_goal = "" if no_props else "dependency:properties "
    # TD verification runs the victim WITH the FlakyCodeChange forcing applied,
    # and that forcing is an injected delay (e.g. Thread.sleep) — so a TD victim
    # is slower here BY CONSTRUCTION than the 180s the other types need.
    # CURATOR-671's victim takes ~207s under the forcing; at 180s surefire kills
    # the fork ("There was a timeout in the fork"), reports Tests run: 0, and
    # even a correct fix is scored FAILED. run_agentic_td.sh's own reproduction
    # passes no timeout at all, which is why the flake reproduces there but the
    # verify could never confirm a repair.
    timeout = ("-Dsurefire.timeout="
               + (os.environ.get("AGENTIC_TD_SUREFIRE_TIMEOUT_S", "900").strip()
                  if test_type == "td" else "180"))

    if test_type == "od":
        # Pair-of-tests with -Dsurefire.runOrder=testorder (TestingResearch-
        # Illinois fork), pinned via SUREFIRE_VERSION.
        # Verification deliberately runs without RV instrumentation. The OD
        # gate only needs to rerun the polluter->victim pair in declared order
        # and check whether the victim now passes.
        # Run `dependency:properties` before the standalone surefire:test goal.
        # Some poms (e.g. ZooKeeper) put `-javaagent:${groupId:artifactId:type}`
        # in surefire's argLine; that property is published by the
        # maven-dependency-plugin:properties goal, which is bound into the
        # lifecycle and so is SKIPPED when surefire:test is invoked directly.
        # Without it the placeholder stays literal, the forked test JVM fails to
        # start ("forked VM terminated without properly saying goodbye"), and
        # verify reports Tests run: 0 regardless of the patch. It is a harmless
        # no-op for projects whose argLine references no such property.
        return (
            cd +
            "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n"
            f"mvn {props_goal}surefire:test "
            f"-pl {module} -Dtest='{polluter},{victim}' "
            f"-Dsurefire.runOrder=testorder {timeout} {MVNOPTS_OD} 2>&1"
        )
    if test_type == "td":
        # TD verify just reruns the failing test on the prepared tree and
        # checks whether it now passes; no RV instrumentation is required.
        # `dependency:properties` first — same reason as the OD branch: it
        # publishes the dependency-path properties that some poms reference in
        # surefire's argLine (e.g. -javaagent:${org.jmockit:jmockit:jar}), which
        # the standalone surefire:test goal would otherwise leave unresolved,
        # crashing the forked test JVM (Tests run: 0). No-op when unused.
        return (
            cd +
            f"mvn {props_goal}surefire:test "
            f"-pl {module} -Dtest='{victim}' {timeout} {MVNOPTS_TD} 2>&1"
        )
    if test_type == "id":
        # NonDex iteration verify. Reads NONDEXSEED + NONDEX_RUNS from env
        # — populated by run_agentic_id.sh from the CSV row.
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip()
        if not seed or not runs:
            sys.exit("ERROR: ID verify requires NONDEXSEED + NONDEX_RUNS env "
                     "vars set by the per-type orchestrator.")
        # NonDex only needs to shuffle iteration orders and run surefire; no RV
        # instrumentation is required.
        nondex_plugin_version = os.environ.get(
            "NONDEX_PLUGIN_VERSION", "2.1.1").strip() or "2.1.1"
        return (
            cd +
            f"mvn edu.illinois:nondex-maven-plugin:{nondex_plugin_version}:nondex "
            f"-DnondexSeed={seed} -DnondexRuns={runs} "
            f"-pl '{module}' "
            f"-Dtest='{victim}' {timeout} {MVNOPTS_ID} 2>&1"
        )
    if test_type == "nio":
        # NIO wrapper-class verify. WRAPPER_FQCN is generated and stashed in
        # the env by run_agentic_nio.sh.
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        if not wrapper:
            sys.exit("ERROR: NIO verify requires WRAPPER_FQCN env var (set "
                     "by run_agentic_nio.sh after wrapper generation).")
        surefire_ver = os.environ.get("SUREFIRE_VER", "3.0.0-M5").strip()
        # The NIO gate only needs to run the wrapper's runTwice() and check
        # that both invocations pass; no RV instrumentation is required.
        return (
            cd +
            f"export SUREFIRE_VERSION={surefire_ver}\n"
            f"mvn test -pl {module} -am "
            f"-Dtest='{wrapper}#runTwice' {timeout} {MVNOPTS_NIO} 2>&1"
        )
    if test_type in ("unclassified", "unassigned"):
        return (
            cd +
            f"mvn test -pl '{module}' -Dtest='{victim}' "
            f"{timeout} {MVNOPTS_TD} 2>&1"
        )
    sys.exit(f"ERROR: unsupported test_type '{test_type}' for agentic verify.")


ReportFingerprint = Tuple[int, int, str]


def _iter_surefire_xml(report_root: Path) -> Iterable[Path]:
    """Yield Surefire XML without descending into unrelated build output."""
    report_root = Path(report_root)
    if not report_root.is_dir():
        return
    for current, dirs, files in os.walk(report_root, onerror=lambda _e: None):
        here = Path(current)
        if here.name == "target":
            dirs[:] = [name for name in dirs if name == "surefire-reports"]
        else:
            dirs[:] = [name for name in dirs
                        if name not in {".git", ".m2", "node_modules"}]
        if here.name != "surefire-reports" or here.parent.name != "target":
            continue
        dirs[:] = []
        for name in files:
            if name.startswith("TEST-") and name.endswith(".xml"):
                yield here / name


def _report_snapshot(report_root: Path) -> Dict[str, ReportFingerprint]:
    snapshot: Dict[str, ReportFingerprint] = {}
    for path in _iter_surefire_xml(report_root):
        try:
            data = path.read_bytes()
            stat = path.stat()
        except OSError:
            continue
        snapshot[str(path)] = (
            stat.st_mtime_ns,
            stat.st_size,
            hashlib.sha256(data).hexdigest(),
        )
    return snapshot


def _fresh_report_files(report_root: Path,
                        before: Dict[str, ReportFingerprint]) -> List[Path]:
    after = _report_snapshot(report_root)
    return [Path(path) for path, fingerprint in after.items()
            if before.get(path) != fingerprint]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _int_attr(element: ET.Element, name: str,
              fallback: Optional[int] = None) -> int:
    raw = element.attrib.get(name)
    if raw is None:
        if fallback is None:
            raise ValueError(f"missing XML attribute {name!r}")
        return fallback
    value = int(raw)
    if value < 0:
        raise ValueError(f"negative XML attribute {name!r}: {value}")
    return value


def _suite_counts(element: ET.Element) -> Dict[str, int]:
    testcases = [node for node in element.iter()
                 if _local_name(node.tag) == "testcase"]
    failures = sum(1 for node in element.iter()
                   if _local_name(node.tag) == "failure")
    errors = sum(1 for node in element.iter()
                 if _local_name(node.tag) == "error")
    skipped = sum(1 for node in element.iter()
                  if _local_name(node.tag) == "skipped")
    return {
        "tests": _int_attr(element, "tests", len(testcases)),
        "failures": _int_attr(element, "failures", failures),
        "errors": _int_attr(element, "errors", errors),
        "skipped": _int_attr(element, "skipped", skipped),
    }


def _parse_xml_reports(paths: Iterable[Path]) -> Tuple[Optional[dict], List[str]]:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    parsed_files: List[str] = []
    errors: List[str] = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()
            root_name = _local_name(root.tag)
            if root_name == "testsuite":
                suites = [root]
            elif all(name in root.attrib
                     for name in ("tests", "failures", "errors")):
                # Some providers emit a testsuites aggregate. Count it once,
                # rather than also summing its child suites.
                suites = [root]
            else:
                suites = [child for child in root
                          if _local_name(child.tag) == "testsuite"]
            if not suites:
                raise ValueError(f"unsupported XML root {root.tag!r}")
            for suite in suites:
                counts = _suite_counts(suite)
                for name in totals:
                    totals[name] += counts[name]
            parsed_files.append(str(path))
        except (OSError, ET.ParseError, ValueError) as exc:
            errors.append(f"{path}: {type(exc).__name__}: {exc}")
    if not parsed_files:
        return None, errors
    totals.update({
        "source": "fresh_surefire_xml",
        "report_files": parsed_files,
    })
    return totals, errors


def _parse_text_summaries(log_text: str) -> dict:
    matches: List[Tuple[int, int, int, int]] = []
    after_results = False
    results_aggregates: List[Tuple[int, int, int, int]] = []
    nonclass: List[Tuple[int, int, int, int]] = []
    class_lines_seen = False
    for line in log_text.splitlines():
        if re.search(r"\bResults\s*:", line):
            after_results = True
        match = SUMMARY_RE.search(line)
        if not match:
            continue
        counts = tuple(int(match.group(i)) for i in range(1, 5))
        matches.append(counts)
        is_class_line = bool(re.search(r"\s-{1,2}\s+in\s+", line))
        class_lines_seen = class_lines_seen or is_class_line
        if (not is_class_line and "<<< FAILURE" not in line
                and "<<< ERROR" not in line):
            nonclass.append(counts)
            if after_results:
                results_aggregates.append(counts)
                after_results = False

    # A Results: block is the strongest aggregate signal. Without it, explicit
    # "- in"/"-- in" class suffixes let us safely select the remaining lines.
    # With neither signal, use the final summary only: old providers sometimes
    # emit indistinguishable per-class and aggregate lines, and summing both is
    # provably inexact. All matches remain secondary failure/skip evidence.
    if results_aggregates:
        selected = results_aggregates
    elif class_lines_seen:
        selected = nonclass
    else:
        selected = matches[-1:]
    return {
        "summary_lines": len(selected),
        "tests": sum(item[0] for item in selected),
        "failures": sum(item[1] for item in selected),
        "errors": sum(item[2] for item in selected),
        "skipped": sum(item[3] for item in selected),
        "evidence_summary_lines": len(matches),
        "evidence_failures": sum(item[1] for item in matches),
        "evidence_errors": sum(item[2] for item in matches),
        "evidence_skipped": sum(item[3] for item in matches),
        "source": "surefire_text" if selected else "none",
    }


def _interpret(log_text: str, returncode: Optional[int] = 0,
               timed_out: bool = False, xml_stats: Optional[dict] = None,
               runner_error: str = "",
               xml_parse_errors: Optional[List[str]] = None) -> tuple[str, dict]:
    """Return an internal tri-state status plus machine-readable evidence."""
    text_stats = _parse_text_summaries(log_text)
    if xml_stats is not None:
        # Fresh XML is the exact count source. Text remains independent
        # defense-in-depth evidence: NonDex and multi-run providers may emit
        # more summaries than their final XML represents, so counts need not
        # be numerically equal, but either source can block a PASS.
        tests = int(xml_stats.get("tests", 0))
        failures = int(xml_stats.get("failures", 0))
        errors = int(xml_stats.get("errors", 0))
        skipped = int(xml_stats.get("skipped", 0))
        source = "fresh_surefire_xml"
    else:
        tests = int(text_stats["tests"])
        failures = int(text_stats["failures"])
        errors = int(text_stats["errors"])
        skipped = int(text_stats["skipped"])
        source = str(text_stats["source"])

    markers = len(MARKER_RE.findall(log_text))
    build_success = BUILD_SUCCESS in log_text
    build_failure = BUILD_FAILURE in log_text
    executed_tests = max(0, tests - skipped)
    text_has_summary = int(text_stats["summary_lines"]) > 0
    text_failure_evidence = (int(text_stats["evidence_failures"]) > 0
                             or int(text_stats["evidence_errors"]) > 0)
    text_skip_evidence = int(text_stats["evidence_skipped"]) > 0
    test_presence_conflict = (
        xml_stats is not None and text_has_summary
        and ((tests > 0) != (int(text_stats["tests"]) > 0))
    )
    parse_errors = list(xml_parse_errors or [])

    if (failures > 0 or errors > 0 or markers > 0
            or text_failure_evidence):
        verdict, reason = FAILED, "executed_test_failure"
    elif timed_out:
        verdict, reason = INCOMPLETE, "verify_timeout"
    elif runner_error or returncode is None:
        verdict, reason = INCOMPLETE, "runner_infrastructure_error"
    elif returncode != 0:
        verdict, reason = INCOMPLETE, "nonzero_verify_exit"
    elif not build_success or build_failure:
        verdict, reason = INCOMPLETE, "maven_build_not_successful"
    elif parse_errors:
        verdict, reason = INCOMPLETE, "malformed_surefire_xml"
    elif test_presence_conflict:
        verdict, reason = INCOMPLETE, "conflicting_test_evidence"
    elif tests <= 0 or (xml_stats is None and text_stats["summary_lines"] <= 0):
        verdict, reason = INCOMPLETE, "no_tests_executed"
    elif skipped > 0 or text_skip_evidence:
        verdict, reason = INCOMPLETE, "tests_skipped"
    else:
        verdict, reason = PASSED, "strict_pass"

    return verdict, {
        "reason": reason,
        "source": source,
        "summary_lines": text_stats["summary_lines"],
        "tests": tests,
        "executed_tests": executed_tests,
        "failures": failures,
        "errors": errors,
        "skipped": skipped,
        "failure_markers": markers,
        "returncode": returncode,
        "timed_out": timed_out,
        "runner_error": runner_error,
        "build_success": build_success,
        "build_failure": build_failure,
        "text_stats": text_stats,
        "xml_stats": xml_stats,
        "xml_parse_errors": parse_errors,
        "test_presence_conflict": test_presence_conflict,
    }


def parse_maven_output(text: str, rc: Optional[int], timed_out: bool = False,
                       fresh_xml_dir: Optional[Path] = None,
                       runner_error: str = "") -> dict:
    """Stable strict parser shared by verifier, launchers, and tests.

    ``fresh_xml_dir`` must contain reports produced by this attempt only. When
    it is omitted or contains no valid ``TEST-*.xml``, parsing falls back to the
    Surefire text summary. ``verdict`` is always public PASSED/FAILED, while
    ``validation_status`` retains the internal INCOMPLETE diagnostic state.
    The returned object is JSON-serializable.
    """
    xml_stats = None
    xml_errors: List[str] = []
    report_files: List[Path] = []
    if fresh_xml_dir is not None:
        xml_root = Path(fresh_xml_dir)
        if xml_root.name == "surefire-reports" and xml_root.is_dir():
            report_files = sorted(xml_root.glob("TEST-*.xml"))
        else:
            report_files = sorted(_iter_surefire_xml(xml_root))
        xml_stats, xml_errors = _parse_xml_reports(report_files)
    validation_status, stats = _interpret(
        text,
        returncode=rc,
        timed_out=timed_out,
        xml_stats=xml_stats,
        runner_error=runner_error,
        xml_parse_errors=xml_errors,
    )
    validation_reason = str(stats["reason"])
    return {
        "verdict": _public_verdict(validation_status),
        "reason": _public_reason(validation_status, validation_reason),
        "validation_status": validation_status,
        "validation_reason": validation_reason,
        "stats": stats,
        "xml_parse_errors": xml_errors,
        "fresh_surefire_xml": [str(path) for path in report_files],
    }


def _report_root(base: Path, row: dict,
                 container_workdir: str = "/app/work/Flaky") -> Optional[Path]:
    """Map a bind-mounted container workdir to its host report root."""
    try:
        relative = PurePosixPath(container_workdir).relative_to("/app/work")
    except ValueError:
        return None
    worktree = Path(base).joinpath(*relative.parts)
    module = (row.get("module") or ".").strip()
    candidate = worktree if module in ("", ".") else worktree / module
    return candidate if candidate.is_dir() else worktree


def _execute_attempt(attempt_number: int, docker_container: str, command: str,
                     report_root: Optional[Path], dependency_properties: bool
                     ) -> Tuple[CommandResult, dict]:
    before = _report_snapshot(report_root) if report_root is not None else {}
    run_result = _run_in_container(docker_container, command)
    fresh_reports = (_fresh_report_files(report_root, before)
                     if report_root is not None else [])
    xml_stats, xml_errors = _parse_xml_reports(fresh_reports)
    validation_status, stats = _interpret(
        run_result.output,
        returncode=run_result.returncode,
        timed_out=run_result.timed_out,
        xml_stats=xml_stats,
        runner_error=run_result.error,
        xml_parse_errors=xml_errors,
    )
    validation_reason = str(stats["reason"])
    record = {
        "attempt": attempt_number,
        "dependency_properties": dependency_properties,
        "command": command,
        "host_report_root": str(report_root) if report_root is not None else "",
        "verdict": _public_verdict(validation_status),
        "reason": _public_reason(validation_status, validation_reason),
        "validation_status": validation_status,
        "validation_reason": validation_reason,
        "run": {
            "returncode": run_result.returncode,
            "timed_out": run_result.timed_out,
            "duration_seconds": run_result.duration_seconds,
            "output_bytes": len(run_result.output.encode("utf-8", "replace")),
            "output_sha256": hashlib.sha256(
                run_result.output.encode("utf-8", "replace")).hexdigest(),
            "error": run_result.error,
            "log_file": "",
            "archive_log_file": "",
        },
        "stats": stats,
        "fresh_surefire_xml": [str(path) for path in fresh_reports],
        "xml_parse_errors": xml_errors,
    }
    return run_result, record


def _attempt_log_text(result: CommandResult) -> str:
    text = result.output
    if result.error:
        suffix = f"[verify infrastructure] {result.error}\n"
        if text and not text.endswith("\n"):
            text += "\n"
        text += suffix
    return text


def _validation_attempt(cli_attempt: Optional[int]) -> Optional[int]:
    raw = cli_attempt
    if raw is None:
        env_raw = (os.environ.get("AGENTIC_VERIFY_ATTEMPT") or "").strip()
        if not env_raw:
            return None
        try:
            raw = int(env_raw)
        except ValueError:
            sys.exit("ERROR: AGENTIC_VERIFY_ATTEMPT must be a positive integer")
    if raw < 1:
        sys.exit("ERROR: --attempt/AGENTIC_VERIFY_ATTEMPT must be >= 1")
    return raw


def _write_immutable_text(path: Path, content: str) -> None:
    """Create an archive artifact exactly once; never replace evidence."""
    with path.open("x", encoding="utf-8") as handle:
        handle.write(content)


def _ensure_archive_slot(archive_dir: Path, validation_attempt: int) -> None:
    prefix = f"attempt_{validation_attempt:02d}"
    collisions = sorted(archive_dir.glob(f"{prefix}*"))
    if collisions:
        names = ", ".join(path.name for path in collisions[:5])
        sys.exit(
            f"ERROR: validation attempt {validation_attempt} is already archived "
            f"({names}); refusing to overwrite verifier evidence"
        )


def _write_command_attempt(steps_dir: Path, archive_dir: Optional[Path],
                           validation_attempt: Optional[int],
                           run_result: CommandResult, record: dict) -> None:
    command_attempt = int(record["attempt"])
    canonical_stem = f"verify_after_fix.attempt_{command_attempt:02d}"
    canonical_log = steps_dir / f"{canonical_stem}.log"
    canonical_json = steps_dir / f"{canonical_stem}.json"
    record["run"]["log_file"] = canonical_log.name

    if archive_dir is not None and validation_attempt is not None:
        archive_stem = (f"attempt_{validation_attempt:02d}."
                        f"command_{command_attempt:02d}")
        archive_log = archive_dir / f"{archive_stem}.log"
        archive_json = archive_dir / f"{archive_stem}.json"
        record["run"]["archive_log_file"] = str(
            archive_log.relative_to(steps_dir))
        _write_immutable_text(archive_log, _attempt_log_text(run_result))
        _write_immutable_text(archive_json, json.dumps(record, indent=2))

    canonical_log.write_text(_attempt_log_text(run_result), encoding="utf-8")
    canonical_json.write_text(json.dumps(record, indent=2), encoding="utf-8")


def _should_retry_without_properties(test_type: str, result: CommandResult,
                                     record: dict) -> bool:
    """Retry only the known pre-test dependency-plugin incompatibility."""
    stats = record.get("stats") or {}
    return (
        test_type in {"od", "td"}
        and record.get("dependency_properties") is True
        and record.get("validation_status") == INCOMPLETE
        and result.returncode not in {None, 0}
        and not result.timed_out
        and int(stats.get("tests", 0)) == 0
        and int(stats.get("failures", 0)) == 0
        and int(stats.get("errors", 0)) == 0
        and int(stats.get("failure_markers", 0)) == 0
        and MISSING_PROPS_GOAL in result.output
    )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container",
                    help="docker container name (default: tm_<container_sanitized>)")
    ap.add_argument("--attempt", type=int,
                    help="validation attempt number to archive without overwrite "
                         "(or set AGENTIC_VERIFY_ATTEMPT)")
    ap.add_argument("--container-workdir", default="/app/work/Flaky",
                    help="project worktree inside the container "
                         "(default: /app/work/Flaky)")
    args = ap.parse_args()
    validation_attempt = _validation_attempt(args.attempt)

    row = load_csv_row(args.container)
    if not row:
        sys.exit(f"ERROR: container '{args.container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type not in {"od", "td", "id", "nio", "unclassified", "unassigned"}:
        sys.exit(f"ERROR: unsupported test_type '{test_type}'")

    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", args.container))

    base = container_run_dir(args.container)
    steps_dir = base / "claude_outputs"
    steps_dir.mkdir(parents=True, exist_ok=True)
    log_path = steps_dir / "verify_after_fix.log"
    verdict_path = steps_dir / "verify_after_fix.verdict"
    result_path = steps_dir / "verify_after_fix.result.json"
    report_root = _report_root(base, row, args.container_workdir)
    archive_dir = None
    if validation_attempt is not None:
        archive_dir = steps_dir / "td_validation" / "runs"
        archive_dir.mkdir(parents=True, exist_ok=True)
        _ensure_archive_slot(archive_dir, validation_attempt)

    cmd = _build_command(
        test_type, row, container_workdir=args.container_workdir)
    print(f"[verify] test_type={test_type}  container={docker_container}")
    attempts = []
    run_result, record = _execute_attempt(
        1, docker_container, cmd, report_root,
        dependency_properties=test_type in {"od", "td"})
    attempts.append(record)
    _write_command_attempt(
        steps_dir, archive_dir, validation_attempt, run_result, record)

    if _should_retry_without_properties(test_type, run_result, record):
        # This project pins maven-dependency-plugin < 2.2, which has no
        # `properties` goal. Nothing ran. Retry without it rather than scoring a
        # FAILED for a Maven configuration mismatch.
        print("[verify] dependency:properties unsupported by this project's "
              "pinned plugin — retrying without it")
        run_result, record = _execute_attempt(
            2, docker_container,
            _build_command(test_type, row, no_props=True,
                           container_workdir=args.container_workdir),
            report_root, dependency_properties=False)
        attempts.append(record)
        _write_command_attempt(
            steps_dir, archive_dir, validation_attempt, run_result, record)

    # Preserve the canonical log as the selected/final attempt for old readers.
    log_path.write_text(_attempt_log_text(run_result), encoding="utf-8")
    verdict = record["verdict"]
    stats = record["stats"]
    verdict_path.write_text(verdict + "\n", encoding="utf-8")
    result_document = {
        "schema_version": 1,
        "validation_attempt": validation_attempt,
        "container": args.container,
        "docker_container": docker_container,
        "test_type": test_type,
        "container_workdir": args.container_workdir,
        "host_report_root": str(report_root) if report_root is not None else "",
        "final_verdict": verdict,
        "final_reason": record["reason"],
        "final_validation_status": record["validation_status"],
        "final_validation_reason": record["validation_reason"],
        "selected_attempt": record["attempt"],
        "selected_command_attempt": record["attempt"],
        "attempts": attempts,
    }
    result_path.write_text(json.dumps(result_document, indent=2), encoding="utf-8")
    if archive_dir is not None and validation_attempt is not None:
        archive_stem = f"attempt_{validation_attempt:02d}"
        _write_immutable_text(
            archive_dir / f"{archive_stem}.log",
            _attempt_log_text(run_result))
        _write_immutable_text(
            archive_dir / f"{archive_stem}.json",
            json.dumps(result_document, indent=2))

    print(f"[verify] summary lines={stats['summary_lines']}  "
          f"Tests={stats['tests']}  Failures={stats['failures']}  "
          f"Errors={stats['errors']}  Skipped={stats['skipped']}  "
          f"markers={stats['failure_markers']}  rc={stats['returncode']}  "
          f"timed_out={stats['timed_out']}  "
          f"build_success={stats['build_success']}")
    print(f"[verify] verdict: {verdict} ({record['reason']}); "
          f"validation_status={record['validation_status']} "
          f"({record['validation_reason']})")
    sys.exit(0 if verdict == PASSED else 1)


if __name__ == "__main__":
    main()
