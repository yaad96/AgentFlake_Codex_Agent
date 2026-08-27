#!/usr/bin/env python3
"""Which TD containers failed for BUILD reasons, not model reasons?

Answers one question: after the three build-robustness fixes land, how many TD
containers are worth re-running?  A container only benefits if its run died on a
Maven gate the fixes remove -- a plugin check we do not skip, a ${argLine} the
forked JVM could not resolve, or an `install` failure where `test-compile` would
still have produced test classes.  Everything else is a real model failure and
re-running it changes nothing.

Reads run artifacts and batch logs only; safe to run mid-batch.  Prints a
per-container verdict with the evidence line that drove the classification, so
you can spot-check rather than trust it.

  REPO=~/AgentFlake_Codex_Agent LOGDIR=~/codex_td_logs python3 td_build_triage.py
"""
import json, os, re, sys
from pathlib import Path

REPO = Path(os.environ.get("REPO", Path.home() / "AgentFlake_Codex_Agent"))
DATA = REPO / "AF_Codex_Agent" / "data"
COHORT = REPO / "AF_Codex_Agent" / "cohorts" / "td.csv"
LOGDIRS = [Path(os.environ.get("LOGDIR", Path.home() / "codex_td_logs")),
           Path(str(os.environ.get("LOGDIR", Path.home() / "codex_td_logs")) + "_dry")]

# Each bucket maps to exactly one of the three fixes, so the counts tell you
# what each fix actually buys.  Ordered: first match wins, most specific first.
SIGNATURES = [
    ("ARGLINE", "fix 1: -DargLine= on the official verify path", [
        r"Could not find or load main class @\{argLine\}",
        r"@\{argLine\}",
        r"Unable to find javaagent",
        r"argLine.*jacoco",
        r"jacocoagent.*not found",
    ]),
    ("PLUGIN_GATE", "fix 2: wider MVNOPTS (spotless/license/findbugs/...)", [
        r"spotless(-maven-plugin)?[:.].*(check|BUILD FAILURE)",
        r"Execution .* of goal .*spotless",
        r"license-maven-plugin|Execution .* of goal .*license",
        r"findbugs-maven-plugin|Execution .* of goal .*findbugs",
        r"spotbugs-maven-plugin|Execution .* of goal .*spotbugs",
        r"modernizer-maven-plugin|Execution .* of goal .*modernizer",
        r"ossindex-maven-plugin|Execution .* of goal .*ossindex",
        r"animal-sniffer|impsort|pgpverify|warbucks",
        r"Execution .* of goal .*(checkstyle|rat|enforcer).*failed",
    ]),
    ("INSTALL_ONLY", "fix 3: mvn test-compile third tier", [
        r"Failed to execute goal .*maven-install-plugin",
        r"BUILD FAILURE.*\n(?:.*\n){0,20}?.*maven-(install|deploy)-plugin",
        r"Could not resolve dependencies for project",
        r"The packaging for this project did not assign a file",
    ]),
]

# A container that simply never reproduced is NOT recoverable by these fixes
# unless one of the signatures above also fired -- the forcing may genuinely not
# perturb it.  Kept separate so it is never silently counted as a win.
NOREPRO = [
    r"did not fail as expected",
    r"FlakyCodeChange did not fail",
    r"no Surefire summary",
    r"EMPTY_PRISTINE_FORCING",
    r"Tests run: 0",
]

COMPILED = [(k, why, [re.compile(p, re.I | re.M) for p in pats])
            for k, why, pats in SIGNATURES]
NOREPRO_RE = [re.compile(p, re.I) for p in NOREPRO]


def containers():
    if COHORT.is_file():
        out = [l.strip() for l in COHORT.read_text().splitlines()[1:] if l.strip()]
        if out:
            return out
    return sorted(p.name for p in DATA.iterdir() if p.is_dir()) if DATA.is_dir() else []


def gather_text(container):
    """Every log that could carry the build failure, newest run first."""
    chunks = []
    cdir = DATA / container
    if cdir.is_dir():
        for rdir in sorted(cdir.glob("run_*"), reverse=True):
            steps = rdir / "codex_outputs"
            for pat in ("*.log", "validation/*.log", "validation/runs/*.log"):
                for f in sorted(steps.glob(pat)):
                    try:
                        chunks.append(f.read_text(errors="replace"))
                    except OSError:
                        pass
    for ld in LOGDIRS:
        if not ld.is_dir():
            continue
        for f in list(ld.glob(f"{container}*.log")) + list(ld.glob(f"*{container}*")):
            if f.is_file():
                try:
                    chunks.append(f.read_text(errors="replace"))
                except OSError:
                    pass
    return "\n".join(chunks)


def verdict_of(container):
    cdir = DATA / container
    if not cdir.is_dir():
        return "NOT_RUN", 0
    runs = sorted(cdir.glob("run_*"))
    if not runs:
        return "NOT_RUN", 0
    best, tokens = "INCOMPLETE", 0
    for rdir in runs:
        steps = rdir / "codex_outputs"
        vf = steps / "run_verdict.txt"
        v = vf.read_text(errors="replace").strip() if vf.is_file() else ""
        if (steps / "usage.json").is_file():
            try:
                u = json.loads((steps / "usage.json").read_text())
                tokens += (u.get("usage") or {}).get("total_tokens", 0) or 0
            except Exception:
                pass
        if v == "PASSED":
            best = "PASSED"
        elif v == "FAILED" and best != "PASSED":
            best = "FAILED"
    return best, tokens


def main():
    rows, buckets = [], {}
    for c in containers():
        v, tok = verdict_of(c)
        if v == "PASSED":
            rows.append((c, v, "-", "already passing", ""))
            continue
        text = gather_text(c)
        hit = None
        for key, why, pats in COMPILED:
            for p in pats:
                m = p.search(text)
                if m:
                    line = next((l.strip() for l in text.splitlines()
                                 if m.group(0).split("\n")[0][:40] in l), m.group(0))
                    hit = (key, why, line[:150])
                    break
            if hit:
                break
        if hit:
            rows.append((c, v, hit[0], hit[1], hit[2]))
            buckets.setdefault(hit[0], []).append(c)
        elif not text:
            rows.append((c, v, "NO_LOGS", "no logs found -- cannot classify", ""))
            buckets.setdefault("NO_LOGS", []).append(c)
        elif any(p.search(text) for p in NOREPRO_RE):
            rows.append((c, v, "NO_REPRO", "forcing did not perturb it", ""))
            buckets.setdefault("NO_REPRO", []).append(c)
        else:
            rows.append((c, v, "MODEL", "genuine repair failure", ""))
            buckets.setdefault("MODEL", []).append(c)

    recoverable = sum(len(buckets.get(k, [])) for k in ("ARGLINE", "PLUGIN_GATE", "INSTALL_ONLY"))
    print(f"TD BUILD TRIAGE   {len(rows)} containers\n")
    print(f"  RE-RUN AFTER THE FIXES : {recoverable}")
    for k, why, _ in SIGNATURES:
        n = len(buckets.get(k, []))
        if n:
            print(f"      {k:<13} {n:>3}   {why}")
    print(f"  no point re-running    : {len(buckets.get('MODEL', []))} model failures, "
          f"{len(buckets.get('NO_REPRO', []))} non-reproducing")
    if buckets.get("NO_LOGS"):
        print(f"  UNCLASSIFIED           : {len(buckets['NO_LOGS'])} (no logs on this machine)")

    for k in ("ARGLINE", "PLUGIN_GATE", "INSTALL_ONLY", "NO_REPRO", "NO_LOGS", "MODEL"):
        if not buckets.get(k):
            continue
        print(f"\n{k}")
        for c, v, kind, why, ev in rows:
            if kind == k:
                print(f"  {c[:60]:<62} {v:<10} {ev}")

    if recoverable:
        names = [c for k in ("ARGLINE", "PLUGIN_GATE", "INSTALL_ONLY") for c in buckets.get(k, [])]
        print("\nRE-RUN LIST (paste into run_td_batch.sh CONTAINERS=())")
        for c in names:
            print(f"  {c}")


if __name__ == "__main__":
    sys.exit(main())
