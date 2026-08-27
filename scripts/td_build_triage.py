#!/usr/bin/env python3
"""Which TD containers failed for BUILD reasons, not model reasons?

Answers one question: after the three build-robustness fixes land, how many TD
containers are worth re-running?  A container only benefits if its build died on
a Maven gate the fixes remove.  Everything else is a real model failure and
re-running it changes nothing.

CLASSIFICATION RULE.  The only trustworthy evidence that a build died on a
plugin is Maven's own failure line:

    [ERROR] Failed to execute goal <group>:<artifact>:<ver>:<goal> ...

Mentions of a plugin in `[INFO] --- foo-plugin:1.2:check ---` (it ran), or in
`Downloading from central: .../foo-plugin.pom` (it was fetched), prove nothing --
an earlier version of this script matched those and produced ~10 false positives.
So: find the failing goal, read its artifactId, and bucket on THAT.  A build that
died on a plugin we cannot skip is reported as NOT recoverable, not silently
counted as a win.

Reads run artifacts and batch logs only; safe to run mid-batch.

  REPO=~/AgentFlake_Codex_Agent LOGDIR=~/codex_td_logs python3 scripts/td_build_triage.py
"""
import json, os, re, sys
from pathlib import Path

REPO = Path(os.environ.get("REPO", Path.home() / "AgentFlake_Codex_Agent"))
DATA = REPO / "AF_Codex_Agent" / "data"
COHORT = REPO / "AF_Codex_Agent" / "cohorts" / "td.csv"
_LD = str(os.environ.get("LOGDIR", Path.home() / "codex_td_logs"))
LOGDIRS = [Path(_LD), Path(_LD + "_dry")]

# Maven's definitive failure line. Everything downstream keys off the artifactId
# it names -- never off a plugin merely appearing in the log.
FAILED_GOAL = re.compile(
    r"Failed to execute goal\s+([\w.\-]+):([\w.\-]+):([\w.\-]+):([\w.\-]+)", re.I)
# Fallback for the shorter prefix-resolved form: "Failed to execute goal foo:bar"
FAILED_GOAL_SHORT = re.compile(r"Failed to execute goal\s+([\w.\-]+):([\w.\-]+)\s", re.I)

# Static-analysis / hygiene plugins that a -D<x>.skip flag disables outright.
# These are what fix 2 (wider MVNOPTS) buys.
SKIPPABLE = {
    "spotless-maven-plugin", "license-maven-plugin", "findbugs-maven-plugin",
    "spotbugs-maven-plugin", "modernizer-maven-plugin", "ossindex-maven-plugin",
    "maven-checkstyle-plugin", "apache-rat-plugin", "maven-enforcer-plugin",
    "animal-sniffer-maven-plugin", "impsort-maven-plugin", "warbucks-maven-plugin",
    "pgpverify-maven-plugin", "maven-pmd-plugin", "japicmp-maven-plugin",
    "maven-javadoc-plugin", "xml-maven-plugin", "dependency-check-maven",
    "maven-dependency-plugin", "forbiddenapis", "checkstyle", "spotbugs",
    "maven-antrun-plugin", "jacoco-maven-plugin", "maven-gpg-plugin",
}
# Fix 3 territory: install/packaging/resolution died, but test sources may still
# compile via `mvn test-compile`.
INSTALLISH = {
    "maven-install-plugin", "maven-deploy-plugin", "maven-shade-plugin",
    "maven-assembly-plugin", "maven-jar-plugin", "maven-war-plugin",
}
# Fix 1 territory: the forked JVM could not start because ${argLine} was unresolved.
ARGLINE_CRASH = [
    re.compile(r"Could not find or load main class @\{argLine\}"),
    re.compile(r"Error: Could not find or load main class @"),
    re.compile(r"Unrecognized option: @\{argLine\}"),
    re.compile(r"The forked VM terminated without properly saying goodbye", re.I),
    re.compile(r"Could not create the Java Virtual Machine", re.I),
]
# A container the forcing never perturbed -- not recoverable by any of the fixes.
NOREPRO = [re.compile(p, re.I) for p in (
    r"did not fail as expected", r"FlakyCodeChange did not fail",
    r"no Surefire summary", r"EMPTY_PRISTINE_FORCING",
)]


def containers():
    if COHORT.is_file():
        out = [l.strip() for l in COHORT.read_text().splitlines()[1:] if l.strip()]
        if out:
            return out
    return sorted(p.name for p in DATA.iterdir() if p.is_dir()) if DATA.is_dir() else []


def gather_text(container):
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
        for f in sorted(set(list(ld.glob(f"{container}*.log")) + list(ld.glob(f"*{container}*")))):
            if f.is_file():
                try:
                    chunks.append(f.read_text(errors="replace"))
                except OSError:
                    pass
    return "\n".join(chunks)


def verdict_of(container):
    cdir = DATA / container
    if not cdir.is_dir() or not list(cdir.glob("run_*")):
        return "NOT_RUN"
    best = "INCOMPLETE"
    for rdir in sorted(cdir.glob("run_*")):
        vf = rdir / "codex_outputs" / "run_verdict.txt"
        v = vf.read_text(errors="replace").strip() if vf.is_file() else ""
        if v == "PASSED":
            return "PASSED"
        if v == "FAILED":
            best = "FAILED"
    return best


def failing_goals(text):
    """Every distinct (artifactId, goal) Maven reported as FAILED."""
    out = []
    for m in FAILED_GOAL.finditer(text):
        out.append((m.group(2), m.group(4), m.group(0)[:150]))
    if not out:
        for m in FAILED_GOAL_SHORT.finditer(text):
            out.append((m.group(1), m.group(2), m.group(0)[:150]))
    seen, uniq = set(), []
    for a, g, raw in out:
        if (a, g) not in seen:
            seen.add((a, g))
            uniq.append((a, g, raw))
    return uniq


def classify(text):
    goals = failing_goals(text)
    if goals:
        for art, goal, raw in goals:
            low = art.lower()
            if any(s in low for s in SKIPPABLE) or low in SKIPPABLE:
                # surefire "There are test failures" is the victim failing, not a gate
                if "surefire" in low:
                    continue
                return "PLUGIN_GATE", f"{art}:{goal}", raw
        for art, goal, raw in goals:
            if art.lower() in INSTALLISH:
                return "INSTALL_ONLY", f"{art}:{goal}", raw
        for art, goal, raw in goals:
            if "surefire" in art.lower() and any(p.search(text) for p in ARGLINE_CRASH):
                return "ARGLINE", f"{art}:{goal}", raw
        art, goal, raw = goals[0]
        if "surefire" in art.lower():
            return "MODEL", f"{art}:{goal} (test failed -- expected)", ""
        return "OTHER_BUILD", f"{art}:{goal}", raw
    if any(p.search(text) for p in ARGLINE_CRASH):
        m = next(p.search(text) for p in ARGLINE_CRASH if p.search(text))
        return "ARGLINE", "forked JVM crash", m.group(0)[:150]
    if any(p.search(text) for p in NOREPRO):
        return "NO_REPRO", "forcing did not perturb it", ""
    if not text.strip():
        return "NO_LOGS", "no logs on this machine", ""
    return "MODEL", "genuine repair failure", ""


RECOVERABLE = ("PLUGIN_GATE", "INSTALL_ONLY", "ARGLINE")
FIXNAME = {
    "ARGLINE": "fix 1: -DargLine= on the official verify path",
    "PLUGIN_GATE": "fix 2: wider MVNOPTS (spotless/license/findbugs/...)",
    "INSTALL_ONLY": "fix 3: mvn test-compile third tier",
}


def main():
    rows, buckets = [], {}
    for c in containers():
        v = verdict_of(c)
        if v == "PASSED":
            rows.append((c, v, "PASSED", "already passing", ""))
            buckets.setdefault("PASSED", []).append(c)
            continue
        kind, why, ev = classify(gather_text(c))
        rows.append((c, v, kind, why, ev))
        buckets.setdefault(kind, []).append(c)

    n_rec = sum(len(buckets.get(k, [])) for k in RECOVERABLE)
    print(f"TD BUILD TRIAGE   {len(rows)} containers")
    print("  (classified ONLY on Maven's 'Failed to execute goal' line)\n")
    print(f"  already passing        : {len(buckets.get('PASSED', []))}")
    print(f"  RE-RUN AFTER THE FIXES : {n_rec}")
    for k in RECOVERABLE:
        if buckets.get(k):
            print(f"      {k:<13} {len(buckets[k]):>3}   {FIXNAME[k]}")
    print(f"  not recoverable        : {len(buckets.get('MODEL', []))} model, "
          f"{len(buckets.get('NO_REPRO', []))} non-reproducing, "
          f"{len(buckets.get('OTHER_BUILD', []))} build failure we cannot skip")
    if buckets.get("NO_LOGS"):
        print(f"  UNCLASSIFIED           : {len(buckets['NO_LOGS'])} (no logs)")

    for k in RECOVERABLE + ("OTHER_BUILD", "NO_REPRO", "NO_LOGS", "MODEL"):
        if not buckets.get(k):
            continue
        print(f"\n{k}")
        for c, v, kind, why, ev in rows:
            if kind == k:
                print(f"  {c[:58]:<60} {v:<10} {why}")
                if ev:
                    print(f"      {ev}")

    if n_rec:
        print("\nRE-RUN LIST (paste into run_td_batch.sh CONTAINERS=())")
        for k in RECOVERABLE:
            for c in buckets.get(k, []):
                print(f"  {c}")


if __name__ == "__main__":
    sys.exit(main())
