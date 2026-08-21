#!/usr/bin/env python3
"""
agentic_codex_cli.py — "Codex agent" repair driver.

Drop-in alternative to agentic_orchestrator.py for the agentic OD pipeline.
Instead of an API conversation that calls submit_patch, this driver runs the
*Codex CLI agent* autonomously inside the already-running tm_<container>
docker container: the agent reproduces the failure, reads/edits the source in
place, and self-verifies by re-running the reproduction command. Its edits are
then captured as a unified diff and scored through the SAME external path the
orchestrator uses (apply_fix.py -> agentic_verify.py), so the result is
directly comparable to ReproFlake/FlakyDoctor.

Invoked by run_agentic_od.sh (with AGENTIC_DRIVER=codex_cli) exactly like the
orchestrator:

    python3 agentic_codex_cli.py <result_container> --docker-container <name>
            [--model gpt-5.4] [--reasoning-effort high]

Preconditions (all set up by run_agentic_od.sh steps 0-9.5):
    - data/<container>/run_<NN>/Flaky/            staged source tree (host bind-mount)
    - data/<container>/run_<NN>/Flaky.pristine/   clean snapshot for restore
    - data/<container>/run_<NN>/traces-flaky/mvn.log   initial failure log
    - container tm_<container> running with narrow binds for Flaky/,
      codex_inputs/ (read-only), and codex_outputs/; protected Fixed and
      forcing-reference trees are deliberately not visible to Codex.
    - Codex CLI installed and OPENAI_API_KEY available on the host.

Outputs under data/<container>/run_<NN>/:
    codex_inputs/ contains prompt_user.txt, prompt_system.txt, and trace_config.json.
    codex_outputs/ contains trial.ndjson, codex.stderr, patch.diff,
    llm_response.json, apply_report.json, verify_after_fix.{log,verdict},
    run_verdict.txt, td_validation/{aggregate,calibration,composition}.json,
    thinking.txt, tool_calls.jsonl, usage.json, and meta.json.
"""

from __future__ import annotations

import argparse
import atexit
from collections import Counter
import hashlib
import json
import os
import re
import shlex
import shutil
import tempfile
import subprocess
import sys
import time
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPROFLAKE_DIR = SCRIPT_DIR.parent
LLM_SCRIPTS_DIR = REPROFLAKE_DIR / "LLM Scripts"
APPLY_FIX = LLM_SCRIPTS_DIR / "apply_fix.py"
AGENTIC_VERIFY = SCRIPT_DIR / "agentic_verify.py"

sys.path.insert(0, str(LLM_SCRIPTS_DIR))
from assemble_llm_context import (  # type: ignore  # noqa: E402
    DATA_DIR,
    load_csv_row,
    fqn_to_path,
    find_source_file,
    extract_java_method,
    extract_failure_from_log,
    _code_mask,
)

sys.path.insert(0, str(SCRIPT_DIR))
from td_oracle import (  # type: ignore  # noqa: E402
    CommandSpec,
    ProtectedTrees,
    RunObservation,
    TestRunOutcome,
    apply_reference_forcing,
    build_oracle,
    calibrate_oracle,
)

# Number of EXTRA passing confirmation runs required after the first PASS —
# the same bar the orchestrator applies (agentic_config.VERIFY_PASS_RUNS), so
# all three systems use one verdict standard. Falls back to 10 if unavailable.
try:
    sys.path.insert(0, str(SCRIPT_DIR))
    from agentic_config import VERIFY_PASS_RUNS  # type: ignore  # noqa: E402
except Exception:
    VERIFY_PASS_RUNS = 10
# Both values below are defaults. --verify-pass-runs / --cli-timeout-s override
# them per run (see main()); left alone, parity with the orchestrator holds.

# Wall-clock cap for the agent run (mvn under emulation is slow).
AGENT_TIMEOUT_S = 2400

# Build artefacts we never want inside the captured patch.
#
# .DS_Store / ._* are macOS Finder droppings, and they are not cosmetic here: a
# .DS_Store anywhere under Flaky/ is captured as a NEW BINARY file, and
# `git apply` then rejects the WHOLE patch --
#     error: cannot apply binary patch to '.DS_Store' without full index line
# -- even when the Java hunk applied cleanly. apply_fix records "no layer landed
# the fix", Flaky/ is restored to pristine, and a perfectly good repair is
# scored FAILED. Applies to every test type, not just TD.
GITIGNORE_BODY = ("target/\n**/target/\n*.class\n*.jar\n*.war\n*.ear\n*.nar\n"
                  ".traces/\ntraces.txt\n.nondex/\n"
                  ".DS_Store\n**/.DS_Store\n._*\n")

EVALUATION_IGNORED_DIRS = {".git", "target", ".gradle", ".idea"}
EVALUATION_IGNORED_FILES = {
    ".DS_Store", ".flattened-pom.xml", "dependency-reduced-pom.xml",
    "pom.xml.versionsBackup",
}


def _evaluation_copy_ignore(source_root: Path):
    """Ignore generated state while retaining source packages named build."""
    source_root = Path(source_root).resolve()

    def _ignore(directory, names):
        try:
            relative_parts = (
                Path(directory).resolve().relative_to(source_root).parts)
        except (OSError, ValueError):
            relative_parts = ()
        return {
            name for name in names
            if name in EVALUATION_IGNORED_DIRS
            or (name == "build" and "src" not in relative_parts)
            or name in EVALUATION_IGNORED_FILES
            or name.startswith("._")
        }

    return _ignore


def strip_binary_hunks(diff: str):
    """Drop binary file sections from a captured patch. Returns (diff, dropped).

    A flaky-test repair is NEVER a binary file, but the agent's tree picks them
    up anyway: macOS .DS_Store, and build output a project writes outside
    target/ (OOZIE puts HDFS mini-cluster images under core/build/test/data/).
    One such hunk makes `git apply` reject the ENTIRE patch --
        error: cannot apply binary patch to '<f>' without full index line
    -- so apply_fix records "no layer landed the fix", Flaky/ is restored to
    pristine, and a perfectly good source fix is scored FAILED. Observed on 4 of
    the TD containers (3x OOZIE via core/build/, 1x COLLECTIONS-812 via
    .DS_Store).

    Filtering by content rather than by directory name is deliberate: an
    ignore-list entry like `build/` would silently discard a real fix in any
    project that keeps sources under that name, which is the same failure we are
    removing here.
    """
    if not diff.strip():
        return diff, []
    kept, dropped = [], []
    for part in re.split(r"(?m)^(?=diff --git )", diff):
        if not part.strip():
            continue
        if "GIT binary patch" in part or re.search(r"(?m)^Binary files .* differ$", part):
            m = re.match(r"diff --git a/(.*?) b/", part)
            dropped.append(m.group(1) if m else "<unknown>")
            continue
        kept.append(part)
    return "".join(kept), dropped


# ---------------------------------------------------------------------------
# TD forced-verify tree transform.
#
# LEGACY COMPATIBILITY ONLY. The official TD path below no longer calls this
# pristine-context textual merge; it uses td_oracle's protected B/P/F/FP,
# reference-first composition. Keep the helper temporarily for downstream
# imports while making its non-authoritative status explicit.
#
# TD flakiness reproduces deterministically ONLY under the FlakyCodeChange
# forcing (a timing perturbation, e.g. Thread.sleep, injected into the victim's
# hot path). The bare victim PASSES alone on Flaky/, so verifying the fix on
# Flaky/ alone scores an empty/no-op patch as a spurious PASSED. We instead
# verify the fix UNDER the forcing.
#
# base/Flaky already holds the agent's fix (apply_fix landed it there), and the
# forcing only ever touches a handful of source files. So we 3-way merge JUST
# those files, in place, per file:
#     base   = ext_baseline/<f>              (pristine)
#     ours   = data/<c>/FlakyCodeChange/<f>  (pristine + forcing)
#     theirs = base/Flaky/<f>                (pristine + agent fix)
# The existing verify path (cd /app/work/Flaky) then runs on (fix + forcing)
# with NO change to agentic_verify.py.
#
# `git merge-file` is the right primitive here. The forcing is a plain
# `diff -ruN` with no index lines, so it must be combined by content, not by
# blob id; merge-file predates every git version we could meet (unlike
# `merge-tree --write-tree`, which needs git >= 2.38); and its exit status is
# unambiguous -- 0 clean, 1..127 = that many conflict hunks, 255 = a real
# error. Distinguishing those three is essential: collapsing them makes an
# environment failure indistinguishable from a genuine overlap, which silently
# turns every TD run into FAILED.
#
# Merging file-by-file also means we never copy the BUILT FlakyCodeChange tree.
# Its target/ dirs are created by the in-container Maven running as root
# (Hadoop/Oozie mini-clusters leave 0700 dirs behind) and are unreadable from
# the host, so a full-tree copy dies with EACCES on exactly those projects.
#
# CONFLICT POLICY: a fix that edits the SAME region the forcing perturbs cannot
# be combined with it textually -- a correct fix often relocates or removes the
# construct the forcing anchors to, so re-injecting the forcing verbatim is
# either impossible or a no-op. We FAIL CLOSED there, naming the file, so the
# case can be triaged instead of silently lost. Note this is a real
# false-negative: `FixedCodeChange/` in the dataset shows the developer's own
# fix coexisting with the forcing for these containers, so a sound tree does
# exist -- it just cannot be recovered by a textual merge.
#
# Returns (ok, reason). ok=True means 'forced tree built; run verify on it'
# (the empty-fix tree lands here and equals FlakyCodeChange => verify FAILS).
# ok=False means the CALLER must force FAILED (fail-closed). It never raises for
# an expected merge/IO problem; an unexpected exception is caught by the caller
# and also mapped to FAILED.
def _td_forcing_targets(forcing_patch, ext_baseline, forcing_tree):
    """Tree-relative paths the FlakyCodeChange forcing changes.

    Prefer the patch's own ---/+++ headers: authoritative, cheap, and it never
    touches the built tree. Fall back to a source-tree diff when the patch is
    not on disk. Build outputs are never merge targets -- the dataset's
    `diff -ruN` of two BUILT trees carries target/ noise that has nothing to do
    with the forcing and, on Hadoop-family projects, is root-owned.
    """
    def _keep(rel):
        parts = Path(rel).parts
        return bool(rel) and not ({"target", "build", ".git"} & set(parts))

    def _strip_tree(p):
        # 'FlakyCodeChange/curator-recipes/src/...' -> 'curator-recipes/src/...'
        return p.split("/", 1)[1] if "/" in p else ""

    rels = []
    patch = Path(forcing_patch) if forcing_patch else None
    if patch and patch.is_file():
        for ln in patch.read_text(errors="replace").splitlines():
            if not (ln.startswith("--- ") or ln.startswith("+++ ")):
                continue
            p = ln[4:].split("\t")[0].strip()
            if not p or p == "/dev/null":
                continue
            rel = _strip_tree(p)
            if _keep(rel) and rel not in rels:
                rels.append(rel)
        if rels:
            return rels

    # Fallback: compare the two source trees directly.
    base_dir, forc_dir = Path(ext_baseline), Path(forcing_tree)
    for root, dirs, files in os.walk(forc_dir):
        dirs[:] = [d for d in dirs if d not in ("target", "build", ".git")]
        for fn in files:
            f = Path(root) / fn
            rel = str(f.relative_to(forc_dir))
            if not _keep(rel):
                continue
            b = base_dir / rel
            try:
                if not b.is_file() or b.read_bytes() != f.read_bytes():
                    rels.append(rel)
            except OSError:
                continue
    return rels


def _td_build_forced_verify_tree(flaky, ext_baseline, forcing_tree,
                                 victim_rel, docker_container="",
                                 container_flaky_path="/app/work/Flaky",
                                 forcing_patch=None):
    flaky = Path(flaky); ext_baseline = Path(ext_baseline)
    forcing_tree = Path(forcing_tree)

    targets = _td_forcing_targets(forcing_patch, ext_baseline, forcing_tree)
    if not targets:
        return (False, "td-merge: the forcing changes no source file (could not "
                       "read FlakyCodeChange.patch and the trees differ only in "
                       "build output) -> FAILED (fail-closed).")

    # ---- Per-file 3-way merge of forcing(ours) and fix(theirs) into Flaky/. --
    merged = []
    for rel in targets:
        b, o, t = ext_baseline / rel, forcing_tree / rel, flaky / rel
        # The forcing ADDS a file: take it verbatim.
        if not b.is_file() and o.is_file():
            t.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(o, t)
            merged.append(rel)
            continue
        # The forcing DELETES a file: honour it unless the fix touched it.
        if b.is_file() and not o.is_file():
            if t.is_file() and t.read_bytes() != b.read_bytes():
                return (False, f"td-merge: the forcing deletes {rel} but the fix "
                               "modified it -> FAILED (fail-closed).")
            if t.is_file():
                t.unlink()
            continue
        if not o.is_file():
            continue
        if not t.is_file():
            return (False, f"td-merge: the fix deleted {rel}, which the forcing "
                           "needs -> FAILED (fail-closed).")
        # The fix left this file alone: the forcing version IS the merge.
        if t.read_bytes() == b.read_bytes():
            shutil.copy2(o, t)
            merged.append(rel)
            continue
        # Both sides changed it -> real 3-way merge. Bytes, not text: Java
        # sources in these projects are not uniformly UTF-8.
        mf = run(["git", "merge-file", "-p",
                  "-L", "forcing", "-L", "pristine", "-L", "fix",
                  str(o), str(b), str(t)],
                 check=False, capture_output=True)
        if mf.returncode == 0:
            t.write_bytes(mf.stdout)
            merged.append(rel)
            continue
        if 0 < mf.returncode < 128:
            # Genuine overlap: the fix edits the region the forcing perturbs.
            return (False, f"td-merge: fix overlaps the forcing region in {rel} "
                           f"({mf.returncode} conflict hunk(s)); no sound "
                           "fix+forcing tree exists -> FAILED (fail-closed).")
        # returncode >= 128 (255 = git error): an ENVIRONMENT failure, not a
        # merge outcome. Surface it -- never report it as an overlap.
        err = (mf.stderr or b"").decode("utf-8", "replace").strip()
        return (False, f"td-merge: git merge-file failed on {rel} "
                       f"(rc={mf.returncode}) -- environment problem, not a "
                       f"conflict: {err[:300]}")

    if not merged:
        return (False, "td-merge: the forcing landed no change in Flaky/ (its "
                       "target files are absent) -> FAILED (fail-closed).")

    # ---- Positive self-check (1): the forcing must have SURVIVED the merge, in
    # EVERY file it touches -- not just the victim test. For roughly half the TD
    # containers the forcing is injected into src/main, so checking only the
    # victim would let a fix silently neutralize it.
    def _norm(s):
        return " ".join(s.split())

    for rel in merged:
        try:
            f_txt = (forcing_tree / rel).read_text(encoding="utf-8", errors="replace")
            live_txt = (flaky / rel).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            return (False, f"td-merge: cannot read merged/forcing {rel}: {e}")
        b = ext_baseline / rel
        p_lines = set(b.read_text(encoding="utf-8", errors="replace").splitlines()) \
            if b.is_file() else set()
        live_norm = _norm(live_txt)
        forcing_added = [ln.strip() for ln in f_txt.splitlines()
                         if ln.strip() and ln not in p_lines
                         and not ln.lstrip().startswith("//")]
        missing = [ln for ln in forcing_added if _norm(ln) not in live_norm]
        if missing:
            return (False, f"td-merge: forcing lines absent from merged {rel} "
                           "(the fix dropped/neutralized the forcing) -> "
                           f"FAILED. missing e.g.: {missing[:2]}")

    # ---- Positive self-check (2): the fix must not have gutted the oracle.
    # NOT "every pristine assertion must survive verbatim": a correct TD fix
    # routinely DELETES the assertion that encodes the timing assumption --
    # COLLECTIONS-812's developer fix and a correct agent fix both remove
    # `assertArrayEquals(expected.toByteArray(), actual.toByteArray(), ...)`,
    # because comparing the timestamped bytes IS the bug. Requiring verbatim
    # survival therefore rejects the ground truth. We only fail closed on total
    # gutting (an oracle-free test passes trivially) and log the rest for audit.
    victim_live = flaky / victim_rel
    victim_pristine = ext_baseline / victim_rel
    if not victim_live.is_file():
        return (False, f"td-merge: victim test file missing after merge "
                       f"({victim_rel})")
    try:
        live_txt = victim_live.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return (False, f"td-merge: cannot read merged victim test: {e}")
    if victim_pristine.is_file():
        p_txt = victim_pristine.read_text(encoding="utf-8", errors="replace")
        live_norm = _norm(live_txt)
        pristine_asserts = [ln.strip() for ln in p_txt.splitlines()
                            if re.search(r"\bassert\w*\s*\(", ln)]
        live_asserts = [ln for ln in live_txt.splitlines()
                        if re.search(r"\bassert\w*\s*\(", ln)]
        if pristine_asserts and not live_asserts:
            return (False, "td-merge: the merged victim test contains NO "
                           "assertions (oracle gutted by the fix) -> FAILED "
                           "(fail-closed).")
        dropped = [a for a in pristine_asserts if _norm(a) not in live_norm]
        if dropped:
            log(f"td-merge: NOTE — the fix removed/rewrote {len(dropped)} of "
                f"{len(pristine_asserts)} pristine assertion(s) in "
                f"{victim_rel}; {len(live_asserts)} assertion(s) remain. "
                f"e.g.: {dropped[:2]}")

    # ---- Defense in depth: never let conflict markers reach a verify build.
    # Only the merged files can carry them, so this stays out of target/.
    for rel in merged:
        try:
            t = (flaky / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "<<<<<<< " in t and ">>>>>>> " in t:
            return (False, f"td-merge: conflict markers survived in {rel}")

    # The merge landed directly in base/Flaky, which is bind-mounted at
    # /app/work/Flaky, so the container already sees (fix + forcing).
    shutil.rmtree(flaky / ".git", ignore_errors=True)
    log(f"td-merge: clean merge of {len(merged)} forced file(s) -> verifying "
        "(fix + forcing); forcing confirmed present, victim oracle non-empty.")
    return (True, "clean-merge: verifying (fix + forcing)")


PRETTY_TYPE = {
    "od": "Order-Dependent (OD)",
    "td": "Test-Dependent (TD)",
    "id": "Implementation-Dependent (ID)",
    "nio": "Non-Idempotent-Outcome (NIO)",
}

# Reproduction/verification commands — kept in lock-step with
# agentic_verify._build_command per type, so the agent self-verifies on the same
# command that produces the official verdict.
MVNOPTS_OD = ('-DfailIfNoTests=false -Dgpg.skip=true -Dcheckstyle.skip '
              '-Drat.skip -Denforcer.skip -Dmaven.javadoc.skip')
MVNOPTS_ID = (
    '-Ddependency-check.skip=true -Dgpg.skip=true -DfailIfNoTests=false '
    '-Dskip.installnodenpm -Dskip.npm -Dskip.yarn -Dlicense.skip '
    '-Dcheckstyle.skip -Drat.skip -Denforcer.skip -Danimal.sniffer.skip '
    '-Dmaven.javadoc.skip -Dfindbugs.skip -Dwarbucks.skip -Dmodernizer.skip '
    '-Dimpsort.skip -Dmdep.analyze.skip -Dpgpverify.skip -Dxml.skip '
    '-Dcobertura.skip=true -Dspotless.skip=true -Dspotless.check.skip=true '
    '-Dossindex.skip=true -Dmaven.bundle.plugin.skip=true '
    '-Dmaven.parallel.force=false')
MVNOPTS_TD = MVNOPTS_OD
MVNOPTS_NIO = MVNOPTS_ID + ' -Dfindbugs.skip=true'

# Per-type initial-failure log directory written by the launcher.
FAILURE_LOG_DIR = {"od": "traces-flaky", "id": "traces-fail",
                   "td": "traces-flakycc", "nio": "traces-flaky"}
SUPPORTED_TYPES = {"od", "id", "td", "nio"}


TD_REPRO_REL = "td_repro.sh"          # written under read-only codex_inputs/
TD_FORCING_SRC_REL = ".td_forcing_src.patch"
# Keep in lock-step with agentic_verify.py's td branch.
TD_SUREFIRE_TIMEOUT_S = os.environ.get("AGENTIC_TD_SUREFIRE_TIMEOUT_S", "900").strip()
COMPILE_TIMEOUT_S = int(os.environ.get("AGENTIC_COMPILE_TIMEOUT_S", "1800"))


def _forcing_source_only(patch_text: str) -> str:
    """The FlakyCodeChange forcing, reduced to its SOURCE hunks.

    The dataset's forcing is a `diff -ruN` of two BUILT trees, so it also carries
    target/ noise and "Binary files ... differ" entries that `patch -F0` cannot
    apply — one of them aborts the whole forcing, which then looks like "the
    forcing does not apply" when the real (source) hunk was fine.

    Process LINE BY LINE, not by splitting on `diff `. `diff -ruN` emits
    standalone "Binary files X and Y differ" lines with NO preceding `diff `
    header — BOOKKEEPER-709's forcing has 50 of them. Splitting on `diff `
    absorbs those lines into the PRECEDING section, so "drop any section
    mentioning Binary files" silently discards the legitimate source hunk that
    happens to sit before them. That yielded an empty forcing and aborted the
    entire run with "FlakyCodeChange.patch ... has no source hunks". A
    standalone binary line CLOSES the current section; it does not invalidate
    it. Same approach as FlakyDoctor's cmds/filter_forcing.py.
    """
    out, keep = [], False
    for line in patch_text.split("\n"):
        if line.startswith("diff "):
            fields = line.split()
            new_path = fields[-1] if len(fields) >= 2 else ""
            rel = new_path.split("/", 1)[1] if "/" in new_path else new_path
            parts = set(Path(rel).parts) if rel else set()
            keep = bool(rel) and rel != "dev/null" \
                and "target" not in parts and ".git" not in parts \
                and not ("build" in parts and "src" not in parts)
            if keep:
                out.append(line)
            continue
        if line.startswith("Binary files ") and line.rstrip().endswith("differ"):
            keep = False
            continue
        if keep:
            out.append(line)
    return "\n".join(out) + ("\n" if out else "")


def install_td_repro_helper(base: Path, inputs: Path,
                            module: str, victim: str) -> bool:
    """Install the public TD reproduction harness under ``codex_inputs``.

    Without this the agent is handed `mvn surefire:test` on the BARE victim in
    /app/work/Flaky — but a TD victim PASSES there by definition; it only fails
    once the FlakyCodeChange timing forcing is applied. So the agent could never
    observe the failure it was asked to fix, and "the victim now passes" was
    satisfied by doing nothing. It burned its whole turn budget probing a green
    test. This gives it the same oracle the scorer uses.

    The harness never patches the agent's live checkout. Each invocation makes
    a disposable source-only copy, applies the public forcing there, compiles
    from scratch, runs the victim, and destroys the copy. This prevents forced
    bytecode from surviving a source restore and contaminating later checks.
    """
    forcing_patch = base / "FlakyCodeChange.patch"
    if not forcing_patch.is_file():
        return False
    src_only = _forcing_source_only(forcing_patch.read_text(errors="replace"))
    if not src_only.strip():
        return False
    inputs.mkdir(parents=True, exist_ok=True)
    (inputs / TD_FORCING_SRC_REL).write_text(src_only, encoding="utf-8")
    (inputs / TD_REPRO_REL).write_text(f"""#!/usr/bin/env bash
# TD reproduction/verification: run the victim UNDER the timing forcing.
#
# A TD victim passes on the bare tree; it fails only with the FlakyCodeChange
# perturbation applied. Running `mvn surefire:test` on Flaky/ alone therefore
# proves nothing. This copies your CURRENT edits into a disposable directory,
# applies the public forcing there, compiles cleanly, and runs the victim.
set -euo pipefail
FLAKY=/app/work/Flaky
FORCING=/app/work/codex_inputs/{TD_FORCING_SRC_REL}
MODULE={shlex.quote(module)}
VICTIM={shlex.quote(victim)}
MVNOPTS="{MVNOPTS_TD}"
# The forcing is an injected delay, so the victim is slower here by construction.
# 180s (what the other types use) kills the fork before CURATOR-671's ~207s run.
SUREFIRE_TIMEOUT={TD_SUREFIRE_TIMEOUT_S}

WORK=$(mktemp -d /tmp/agentflake-td-repro.XXXXXX)
cleanup() {{ rm -rf "$WORK"; }}
trap cleanup EXIT INT TERM
mkdir -p "$WORK/Flaky"
# Exclude generated bytecode and VCS metadata. A fresh copy makes source
# mtimes and any previously compiled forced classes irrelevant.
tar -C "$FLAKY" \
  --exclude='.git' --exclude='*/.git' \
  --exclude='target' --exclude='*/target' \
  --exclude='.nondex' --exclude='*/.nondex' \
  --exclude='.traces' --exclude='*/.traces' \
  --exclude='.DS_Store' --exclude='*/.DS_Store' \
  -cf - . | tar -C "$WORK/Flaky" -xf -
cd "$WORK/Flaky"

if ! patch -p1 -F0 --no-backup-if-mismatch -s -i "$FORCING" 2>/dev/null; then
  echo "TD-REPRO: the timing forcing does NOT apply on top of your current edits."
  echo "TD-REPRO: this public textual probe is unavailable for this edit."
  echo "TD-REPRO: do not contort a root-cause fix merely to preserve its patch"
  echo "TD-REPRO: anchor; official validation uses a protected forcing adapted"
  echo "TD-REPRO: from the developer-fixed context."
  exit 2
fi

echo "TD-REPRO: forcing applied in a disposable checkout; building cleanly"
# Install reactor dependencies because surefire:test is a separate Maven
# invocation; test-compile alone can leave it resolving stale sibling jars.
timeout -k 30s {COMPILE_TIMEOUT_S}s \
  mvn -B -ntp install -DskipTests -pl "$MODULE" -am $MVNOPTS 2>&1
echo "TD-REPRO: running the victim UNDER the forcing (this is the real oracle)"
# `dependency:properties` resolves argLine placeholders some poms need, but the
# goal only exists in maven-dependency-plugin >= 2.2. Projects pinning an older
# one (Apex pins 2.1) fail instantly with "Could not find goal 'properties'" and
# run no tests, so fall back to plain surefire:test on that error.
set +e
OUT=$(timeout -k 30s $((SUREFIRE_TIMEOUT + 300))s \\
      mvn -B -ntp dependency:properties surefire:test -pl "$MODULE" -Dtest="$VICTIM" \\
      -Dsurefire.timeout=$SUREFIRE_TIMEOUT $MVNOPTS 2>&1)
TEST_RC=$?
set -e
if [[ "$TEST_RC" -ne 0 ]] && \
   grep -q "Could not find goal 'properties' in plugin" <<<"$OUT"; then
  echo "TD-REPRO: this project pins maven-dependency-plugin < 2.2; retrying without dependency:properties"
  set +e
  OUT=$(timeout -k 30s $((SUREFIRE_TIMEOUT + 300))s \\
        mvn -B -ntp surefire:test -pl "$MODULE" -Dtest="$VICTIM" \\
        -Dsurefire.timeout=$SUREFIRE_TIMEOUT $MVNOPTS 2>&1)
  TEST_RC=$?
  set -e
fi
printf '%s\\n' "$OUT"
exit "$TEST_RC"
""", encoding="utf-8")
    (inputs / TD_REPRO_REL).chmod(0o755)
    return True


def repro_command(test_type: str, module: str, polluter: str, victim: str) -> str:
    # Always recompile FIRST (the test runner executes compiled .class files),
    # then run the type-specific check.
    if test_type == "id":
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip() or "1"
        ver = os.environ.get("NONDEX_PLUGIN_VERSION", "2.1.1").strip() or "2.1.1"
        return (
            f"mvn test-compile -pl {module} {MVNOPTS_ID} 2>&1   # 1) recompile your edits\n"
            f"mvn edu.illinois:nondex-maven-plugin:{ver}:nondex "
            f"-DnondexSeed={seed} -DnondexRuns={runs} "
            f"-pl '{module}' -Dtest='{victim}' -Dsurefire.timeout=180 {MVNOPTS_ID} 2>&1   # 2) run NonDex"
        )
    if test_type == "td":
        # NOT the bare victim: it PASSES on the unforced tree, so it can never
        # show the failure and "it passes now" would be true of an empty fix.
        # td_repro.sh applies the FlakyCodeChange forcing over the current edits,
        # runs the victim, and restores the forced files.
        return (
            f"bash /app/work/codex_inputs/{TD_REPRO_REL}   # uses a clean "
            f"disposable copy, applies the timing forcing, recompiles, and runs "
            f"the victim under it"
        )
    if test_type == "nio":
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        ver = os.environ.get("SUREFIRE_VER", "3.0.0-M5").strip() or "3.0.0-M5"
        return (
            f"export SUREFIRE_VERSION={ver}\n"
            f"mvn test-compile -pl {module} -am {MVNOPTS_NIO} 2>&1   # 1) recompile your edits\n"
            f"mvn test -pl {module} -am -Dtest='{wrapper}#runTwice' "
            f"-Dsurefire.timeout=180 {MVNOPTS_NIO} 2>&1   # 2) run the test twice"
        )
    # od
    return (
        "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n"
        f"mvn test-compile -pl {module} {MVNOPTS_OD} 2>&1   # 1) recompile your edits\n"
        f"mvn dependency:properties surefire:test "
        f"-pl {module} -Dtest='{polluter},{victim}' "
        f"-Dsurefire.runOrder=testorder -Dsurefire.timeout=180 {MVNOPTS_OD} 2>&1   # 2) run"
    )


def type_context(test_type: str):
    """(order_phrase, type_note) injected into the system prompt per type."""
    if test_type == "id":
        seed = os.environ.get("NONDEXSEED", "").strip()
        runs = os.environ.get("NONDEX_RUNS", "").strip() or "1"
        note = (
            "This is an Implementation-Dependent (ID) flaky test: it fails "
            "non-deterministically because it relies on an unspecified "
            "iteration/element order. NonDex re-runs the test under shuffled "
            "orders to expose this. There is NO polluter test — the root cause "
            "is the test (or the code it exercises) assuming an order that is "
            "not guaranteed. Your fix must make it pass regardless of order "
            "(e.g. sort results, use order-stable collections, or drop the "
            "order assumption).\n\n")
        return (f"across the NonDex shuffled run(s) (pinned seed {seed}, "
                f"{runs} run(s))"), note
    if test_type == "td":
        note = (
            "This is a Timing-Dependent (TD) flaky test: it has a latent race / "
            "async / timing assumption. Run ON ITS OWN ON THE UNMODIFIED TREE IT "
            "PASSES — so a plain `mvn test` proves nothing here. The failure is "
            "made deterministic by a timing perturbation (the FlakyCodeChange "
            "'forcing', e.g. an injected Thread.sleep) applied to the code the "
            "test depends on. The reproduction command below applies that forcing "
            "for you and runs the victim under it; that is the ONLY oracle that "
            "distinguishes a real fix from a no-op. Fix the underlying timing "
            "assumption so the test passes even under the perturbation. The "
            "public reproduction script is a diagnostic probe; official scoring "
            "uses a protected equivalent derived in the developer-fixed context, "
            "so preserving a textual patch anchor is not a requirement. Do not "
            "weaken assertions or special-case the injected delay.\n\n")
        return "under the timing forcing (see the reproduction command)", note
    if test_type == "nio":
        wrapper = os.environ.get("WRAPPER_FQCN", "").strip()
        note = (
            "This is a Non-Idempotent-Outcome (NIO) flaky test: it passes the "
            "first time but FAILS when run a second time in the same JVM, because "
            "it leaves behind state (static fields, files, singletons, system "
            "properties, registered hooks, etc.). A generated wrapper class "
            f"({wrapper}) runs the victim twice via #runTwice. Make the SECOND run "
            "pass too — typically by resetting/cleaning up the shared state in "
            "setUp/tearDown or making the code idempotent. Do NOT edit the "
            "generated wrapper class.\n\n")
        return "when run twice in a row (the wrapper's #runTwice)", note
    return "deterministically under this test order", ""


SYSTEM_PROMPT_TMPL = """\
You are an expert Java developer who diagnoses and repairs flaky tests, working
directly inside the project's working directory (your current directory).

GOAL — make the named flaky test pass deterministically with the SMALLEST
correct change. Do NOT rename methods, change unrelated code, modify assertions
to mask a real bug, or refactor the test. Success = the project compiles AND
the victim test passes under the exact reproduction command below.

{type_note}You work with your own tools (there is no submit_patch / get_code tool):
  - Read the victim test, the polluter test (if any), and any related source
    files you need.
  - Run the reproduction command in the shell and observe the real result.
    Never conclude from reasoning alone that the test now passes.
  - Apply the minimal source change directly to the files on disk. Do NOT
    print a diff or a patch as your answer and do NOT write a patch file —
    your change is captured from the working tree afterwards, so an edit that
    only exists in your message counts as no fix at all.

Reproduction / self-verification commands (run them from the project root, which
is your current directory). ALWAYS run the recompile step after editing source —
the test runner executes compiled .class files, so without recompiling it would
use stale bytecode and not reflect your change:

{repro}

A run PASSES iff Surefire reports Tests>0, Failures=0, Errors=0 and there are
no "<<< FAILURE" / "<<< ERROR" markers in the output. First reproduce the
failure to see it firsthand, then make the minimal fix, then recompile and re-run
to confirm the victim now passes {order_phrase}. When
the fix is confirmed, end your final message with the single line: DONE.
"""

USER_PROMPT_TMPL = """\
=== AGENTIC FLAKY-TEST REPAIR TASK ===

GOAL: Diagnose and fix the flaky test below with the SMALLEST possible
change so that the project compiles and the test passes deterministically
under the reproduction command. Do NOT rename, refactor, or reformat
unrelated code. Do NOT modify assertions or test logic unless the assertion
itself is the root cause.

=== TEST CASE ===
Category:   {pretty_type}
Container:  {container}
{polluter_line}Victim:     {victim_fqn}
Module:     {module}
{java_line}
=== TEST CODE ===
{test_code}

=== INITIAL FAILURE LOG ===
{failure_text}

=== HOW TO PROCEED ===
  1. Run the reproduction command to observe the failure firsthand.
  2. Reason from the test code and failure log above; Read related source only
     as needed.
  3. Make the smallest edit consistent with the evidence.
  4. Re-run the reproduction command to confirm the victim now passes
     deterministically {order_phrase}.
  5. End your final message with the single line: DONE.
"""


def log(msg: str) -> None:
    print(f"[codex-cli] {msg}", flush=True)


def run(cmd, **kw):
    """subprocess.run wrapper that streams nothing but returns the result."""
    return subprocess.run(cmd, **kw)


def git(work_tree: Path, *args: str, gitdir: Path = None, check: bool = True):
    """Run git against `work_tree`. With gitdir set, the repo metadata lives at
    an EXTERNAL path (outside the agent's reach); otherwise a work_tree-local
    .git is used. GIT_CEILING_DIRECTORIES forbids git from ever walking up into
    the outer Valg repo (which would otherwise stage our edits there)."""
    env = dict(os.environ)
    env["GIT_CEILING_DIRECTORIES"] = str(Path(work_tree).resolve().parent)
    ident = ["-c", "user.name=agent", "-c", "user.email=agent@local"]
    if gitdir is not None:
        env["GIT_DIR"] = str(gitdir)
        env["GIT_WORK_TREE"] = str(work_tree)
        cmd = ["git", *ident, *args]
    else:
        cmd = ["git", "-C", str(work_tree), *ident, *args]
    return run(cmd, env=env, check=check, capture_output=True, text=True)

class RestoreTreeError(RuntimeError):
    pass


def _path_snapshot(path: Path, limit: int = 12) -> str:
    """Small ownership/mode sample for restore failures."""
    if not path.exists():
        return "(path no longer exists)"
    rows = []
    try:
        for i, p in enumerate(path.rglob("*")):
            if i >= limit:
                rows.append("...")
                break
            try:
                st = p.lstat()
                rel = p.relative_to(path)
                rows.append(
                    f"{rel} mode={oct(st.st_mode & 0o777)} "
                    f"uid={st.st_uid} gid={st.st_gid}")
            except OSError as exc:
                rows.append(f"{p}: {exc}")
    except OSError as exc:
        rows.append(f"(could not list contents: {exc})")
    return "; ".join(rows) if rows else "(empty directory)"


def _reclaim_container_path(docker_container: str, container_path: str) -> str:
    """Make a bind-mounted container path removable by the host user.

    Maven/Codex run as root inside Docker and can leave root-owned target/
    files under the bind mount. On Linux, host-side shutil.rmtree cannot remove
    those files until ownership/mode are repaired from inside the container.
    """
    if not docker_container or not hasattr(os, "getuid") or not hasattr(os, "getgid"):
        return ""
    uid, gid = os.getuid(), os.getgid()
    q = shlex.quote(container_path)
    script = (
        f"if [ -e {q} ]; then "
        f"chown -R {uid}:{gid} {q} && chmod -R u+rwX {q}; "
        "fi"
    )
    proc = run(["docker", "exec", "-u", "0", docker_container, "sh", "-lc", script],
               check=False, capture_output=True, text=True)
    if proc.returncode == 0:
        return ""
    return ((proc.stderr or proc.stdout or "").strip()
            or f"docker exec chown returned {proc.returncode}")


def _remove_evaluator_git_metadata(work_tree: Path) -> None:
    """Remove the local Git repository once patch application is complete.

    The repository is created solely so apply_fix.py can target this worktree.
    Keeping it through Docker compilation makes ownership reclamation recurse
    into host-created pack files, which Docker Desktop cannot chown and which
    would turn a successful Maven compile into CANDIDATE_COMPILE_FAILED.
    """
    git_path = Path(work_tree) / ".git"
    try:
        if git_path.is_symlink() or git_path.is_file():
            git_path.unlink()
        elif git_path.is_dir():
            shutil.rmtree(git_path)
    except OSError as exc:
        raise RestoreTreeError(
            f"could not remove evaluator Git metadata at {git_path}: {exc}") from exc
    if git_path.exists() or git_path.is_symlink():
        raise RestoreTreeError(
            f"evaluator Git metadata still exists after removal: {git_path}")


def _replace_tree_from(source: Path, dest: Path, *, docker_container: str = "",
                       container_dest: str = "", symlinks: bool = False,
                       label: str = "tree restore") -> None:
    """Replace dest with source without silently ignoring deletion failures."""
    source = Path(source)
    dest = Path(dest)
    if not source.is_dir():
        raise RestoreTreeError(f"{label}: source tree missing: {source}")

    notes = []
    if dest.exists():
        for attempt in (1, 2):
            if docker_container and container_dest:
                note = _reclaim_container_path(docker_container, container_dest)
                if note:
                    notes.append(f"ownership repair attempt {attempt}: {note}")
            try:
                shutil.rmtree(dest)
            except Exception as exc:
                notes.append(f"rmtree attempt {attempt}: {exc!r}")
                continue
            break

    if dest.exists():
        detail = _path_snapshot(dest)
        msg = [
            f"{label}: failed to remove existing tree before restore: {dest}",
            "This usually means Docker left root-owned files in the bind mount.",
            f"remaining entries: {detail}",
        ]
        if notes:
            msg.append("attempts: " + " | ".join(notes))
        raise RestoreTreeError("\n".join(msg))

    try:
        shutil.copytree(source, dest, symlinks=symlinks)
    except Exception as exc:
        raise RestoreTreeError(
            f"{label}: failed to copy {source} -> {dest}: {exc!r}") from exc


def build_test_code(base: Path, module: str, fqns) -> str:
    """Extract the source of each victim/polluter method, concatenated."""
    chunks = []
    for fqn in fqns:
        if not fqn:
            continue
        rel_path, method = fqn_to_path(fqn)
        src = find_source_file(str(base), module, rel_path)
        if not src:
            chunks.append(f"// ({fqn}) — source file not found under Flaky/")
            continue
        body = extract_java_method(src, method) if method else None
        if not body:
            chunks.append(f"// ({fqn}) — method '{method}' not found in {rel_path}")
            continue
        chunks.append(f"// ===== {fqn} ({rel_path}) =====\n{body}")
    return "\n\n".join(chunks) if chunks else "(no test code extracted)"


def assemble_prompts(row: dict, base: Path):
    test_type = (row.get("test_type") or "").strip().lower()
    module = (row.get("module") or ".").strip()
    polluter = (row.get("polluter/state setter") or "").strip()
    victim = (row.get("flaky_test") or "").strip()
    java = (row.get("java") or "").strip()
    container = (row.get("result_container") or "").strip()

    test_code = build_test_code(base, module, [victim, polluter])
    log_dir = FAILURE_LOG_DIR.get(test_type, "traces-flaky")
    failure_text = extract_failure_from_log(str(base / log_dir / "mvn.log"))

    polluter_line = f"Polluter:   {polluter}\n" if polluter else ""
    java_line = f"Java:       {java}\n" if java else ""
    order_phrase, type_note = type_context(test_type)

    user_prompt = USER_PROMPT_TMPL.format(
        pretty_type=PRETTY_TYPE.get(test_type, test_type.upper() or "Flaky"),
        order_phrase=order_phrase,
        container=container,
        polluter_line=polluter_line,
        victim_fqn=victim,
        module=module,
        java_line=java_line,
        test_code=test_code,
        failure_text=failure_text,
    )
    system_prompt = SYSTEM_PROMPT_TMPL.format(
        type_note=type_note,
        order_phrase=order_phrase,
        repro="  " + repro_command(
            test_type, module, polluter, victim).replace("\n", "\n  "))
    return user_prompt, system_prompt


SIMULATED_AGENT_ENV = "AGENTIC_SIMULATED_AGENT"


def run_simulated_agent(docker_container: str, base: Path, output_rel: str,
                        fixture_dir: Path) -> int:
    """Replay a recorded agent instead of calling the Codex API. Zero cost.

    The seam is deliberately placed where the real agent's ONLY durable effect
    is: edits to /app/work/Flaky. Everything downstream -- patch capture, the
    binary-hunk strip, apply_fix, the td_oracle forcing composition, compile,
    verify, and verdict aggregation -- then runs for real against a real Maven
    build. That is what makes this a pipeline test rather than a mock.

    Fixture layout (any subset):
        patch.diff     applied to Flaky/ with patch(1); absent or empty models a
                       no-op agent, which the oracle MUST score FAILED
        trial.ndjson   pre-recorded stream; a minimal one is synthesized if absent
        exit_code      integer agent exit status; defaults to 0

    Because the fixture patch lands in Flaky/ exactly as a live agent's edits
    would, the 22 stored patch.diff files from the original batch can be replayed
    verbatim as real model outputs.
    """
    fixture_dir = Path(fixture_dir)
    if not fixture_dir.is_dir():
        sys.exit(f"ERROR: {SIMULATED_AGENT_ENV}={fixture_dir} is not a directory.")
    out_dir = base / output_rel
    out_dir.mkdir(parents=True, exist_ok=True)

    patch_src = fixture_dir / "patch.diff"
    patch_text = patch_src.read_text(errors="replace") if patch_src.is_file() else ""
    if patch_text.strip():
        # Stage inside output_rel, NOT the run dir root: step 10 restarts the
        # container with a ground-truth-free mount set (only Flaky/,
        # codex_inputs/, codex_outputs/), so the run dir root is not visible
        # from inside the container.
        staged = out_dir / ".simulated_agent.patch"
        staged.write_text(patch_text, encoding="utf-8")
        # patch(1), not `git apply`: the run dir may sit inside an outer git repo
        # that ignores data/, and git apply silently SKIPS ignored paths.
        proc = run(["docker", "exec", docker_container, "bash", "-lc",
                    "cd /app/work/Flaky && patch -p1 --no-backup-if-mismatch "
                    f"-i /app/work/{output_rel}/{staged.name}"],
                   check=False, capture_output=True, text=True)
        if proc.returncode != 0:
            # A fixture that will not apply is a broken experiment, not a model
            # failure -- surface it instead of silently scoring an empty patch.
            sys.exit(f"ERROR: simulated agent patch did not apply "
                     f"(rc={proc.returncode}): "
                     f"{(proc.stdout or '')[-400:]}{(proc.stderr or '')[-400:]}")
        log(f"simulated agent: applied {len(patch_text)} byte fixture patch to Flaky/")
    else:
        log("simulated agent: no fixture patch — modelling a no-op agent "
            "(the oracle must score this FAILED)")

    stream_src = fixture_dir / "trial.ndjson"
    if stream_src.is_file():
        shutil.copy2(stream_src, out_dir / "trial.ndjson")
    else:
        (out_dir / "trial.ndjson").write_text("\n".join(json.dumps(rec) for rec in (
            {"type": "thread.started", "thread_id": "simulated"},
            {"type": "turn.started"},
            {"type": "item.completed", "item": {
                "id": "sim-reasoning-0", "type": "reasoning",
                "text": f"simulated agent: {fixture_dir.name}"}},
            {"type": "turn.completed", "usage": {
                "input_tokens": 0, "cached_input_tokens": 0,
                "cache_write_input_tokens": 0, "output_tokens": 0,
                "reasoning_output_tokens": 0}},
        )) + "\n", encoding="utf-8")
    (out_dir / "codex.stderr").write_text("", encoding="utf-8")

    rc_file = fixture_dir / "exit_code"
    try:
        return int(rc_file.read_text().strip()) if rc_file.is_file() else 0
    except ValueError:
        return 0


def run_agent_in_container(docker_container: str, model: str,
                           reasoning_effort, max_turns,
                           input_rel: str, output_rel: str) -> tuple[int, int]:
    """docker exec the Codex agent inside /app/work/Flaky.

    Returns (exit_code, wall_clock_ms). The agent edits the checkout in place;
    its change is captured from git afterwards, so nothing here depends on the
    agent reporting a patch.
    """
    if max_turns:
        # `codex exec` runs a single turn whose internal tool loop is unbounded,
        # and Codex exposes no cost ceiling. Say so rather than silently
        # dropping a limit the caller asked for. The effective bound is the
        # container-side `timeout` below.
        log(f"WARNING: --max-iterations/AGENTIC_MAX_ITERATIONS={max_turns} is "
            "ignored: codex exec has no turn or budget cap. The effective "
            f"bound is the {AGENT_TIMEOUT_S}s agent timeout.")

    # Build the codex argv explicitly rather than with backslash line
    # continuations: inside a Python f-string a trailing "\" would be eaten as a
    # Python line continuation, silently joining the shell lines.
    codex_flags = ["--model", shlex.quote(str(model))]
    if reasoning_effort:
        codex_flags += [
            "-c", shlex.quote(f"model_reasoning_effort={reasoning_effort}")]
    codex_flags += ["--dangerously-bypass-approvals-and-sandbox",
                    "--skip-git-repo-check", "--json"]
    flag_str = " ".join(codex_flags)

    out = f"/app/work/{output_rel}"
    inner = f"""
set -o pipefail
export PATH="/root/.local/bin:$PATH"

# Per-run Codex home. Isolates auth + session state, and — because Codex reads
# AGENTS.md from $CODEX_HOME — lets the system prompt live OUTSIDE the
# repository. An AGENTS.md written into Flaky/ would be picked up by the
# `git diff` patch capture and contaminate every candidate patch.
export CODEX_HOME="$(mktemp -d)"
cp "/app/work/{input_rel}/prompt_system.txt" "$CODEX_HOME/AGENTS.md"

: > {out}/codex.stderr

# Non-interactive auth: a VM/container has no browser for `codex login`.
printenv OPENAI_API_KEY | codex login --with-api-key >>{out}/codex.stderr 2>&1 || {{
  echo "ERROR: 'codex login --with-api-key' failed" >>{out}/codex.stderr
  exit 97
}}

cd /app/work/Flaky
# --dangerously-bypass-approvals-and-sandbox: the Docker container IS the
#   sandbox. Codex's own Landlock/seccomp layer is redundant here and can
#   break Maven when nested inside Docker.
# --skip-git-repo-check: the driver deletes Flaky/.git and keeps the baseline
#   repo at an external GIT_DIR, so the agent's cwd is deliberately not a repo.
timeout -k 30s {AGENT_TIMEOUT_S}s codex exec "$(cat /app/work/{input_rel}/prompt_user.txt)" {flag_str} > {out}/trial.ndjson 2>> {out}/codex.stderr
"""
    # Auth for the in-container `codex` CLI. Prefer an explicit env export;
    # otherwise fall back to AF_Codex_Agent/.openai_api_key via
    # agentic_config.OPENAI_API_KEY. Without this the agent runs
    # UNAUTHENTICATED and silently emits an empty patch that is then misscored
    # as a FAILED repair. Fail closed with a clear message instead of burning
    # a run.
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        try:
            from agentic_config import OPENAI_API_KEY as _CFG_KEY  # type: ignore  # noqa: E402
            api_key = (_CFG_KEY or "").strip()
        except Exception:
            api_key = ""
    if not api_key:
        sys.exit("ERROR: no OPENAI_API_KEY in the environment or "
                 "AF_Codex_Agent/.openai_api_key — the Codex agent cannot "
                 "authenticate and would emit an empty patch scored as a "
                 "false FAILED. Export OPENAI_API_KEY or put the key in "
                 "AF_Codex_Agent/.openai_api_key, then re-run.")
    cmd = ["docker", "exec",
           "-e", f"OPENAI_API_KEY={api_key}",
           docker_container, "bash", "-c", inner]
    effort_msg = f", effort={reasoning_effort}" if reasoning_effort else ""
    log(f"running Codex agent in {docker_container} (model={model}"
        f"{effort_msg}, timeout={AGENT_TIMEOUT_S}s)")
    started = time.monotonic()
    try:
        # Host-side timeout is a backstop only — the container-side `timeout`
        # above is the real one (it actually kills codex inside the container,
        # whereas killing the `docker exec` client would orphan it). Give the
        # host a grace margin so the in-container kill fires first.
        proc = run(cmd, timeout=AGENT_TIMEOUT_S + 120)
        rc = proc.returncode
    except subprocess.TimeoutExpired:
        log(f"agent exceeded {AGENT_TIMEOUT_S + 120}s host wall-clock — killed")
        rc = 124
    elapsed_ms = int((time.monotonic() - started) * 1000)
    if rc == 97:
        log("codex login failed inside the container — see codex.stderr")
    return rc, elapsed_ms


def quiesce_agent_processes(docker_container: str) -> None:
    """Stop orphaned agent/tool processes before inspecting or scoring edits.

    Codex can finish while a background Bash/Maven/Java child remains alive.
    Such a child could keep changing the checkout during patch capture or read
    later evaluation state. Freeze then kill every process except the container
    init and this cleanup exec. A name allow-list is intentionally insufficient:
    candidate-controlled build code could launch Python, Perl, sleep, or a
    renamed binary and outlive Maven.
    """
    script = r'''
set -eu
me=$$
parent=$PPID
victims=""
snapshot="$(ps -eo pid=,stat=,comm=)"
while read -r pid stat comm; do
  [ "$pid" = "1" ] && continue
  [ "$pid" = "$me" ] && continue
  [ "$pid" = "$parent" ] && continue
  case "$stat" in Z*) continue ;; esac
  kill -0 "$pid" 2>/dev/null || continue
  victims="$victims $pid"
done <<< "$snapshot"
if [ -n "$victims" ]; then
  kill -STOP $victims 2>/dev/null || true
  kill -KILL $victims 2>/dev/null || true
fi
remaining=""
snapshot="$(ps -eo pid=,stat=,comm=)"
while read -r pid stat comm; do
  [ "$pid" = "1" ] && continue
  [ "$pid" = "$me" ] && continue
  [ "$pid" = "$parent" ] && continue
  case "$stat" in Z*) continue ;; esac
  kill -0 "$pid" 2>/dev/null || continue
  remaining="$remaining $pid:$comm"
done <<< "$snapshot"
[ -z "$remaining" ] || {
  echo "agent/tool processes still alive:$remaining" >&2
  exit 1
}
'''
    proc = run(
        ["docker", "exec", "-u", "0", docker_container, "bash", "-c", script],
        check=False, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(
            "could not quiesce agent child processes before protected "
            f"validation (rc={proc.returncode}): {detail}")


def compile_in_container(docker_container: str, test_type: str, module: str,
                         *, container_workdir: str = "/app/work/Flaky",
                         clean: bool = False, log_path: Path | None = None):
    """Ensure the exact candidate artifacts exist before verification.

    apply_fix.py only recompiles when it APPLIES a patch, and the OD/TD verify
    command (surefire:test) does not compile. So a run where the agent correctly
    submits no patch (the test already passes) would otherwise be verified
    against absent/stale test classes -> Tests=0 -> a spurious FAILED, and a
    patch-submitting system would unfairly recompile-and-pass where a no-patch
    one fails. Compiling here makes the verdict reflect reality for both.

    TD uses ``install -DskipTests -am`` rather than only ``test-compile``:
    standalone Surefire starts a new Maven invocation, so sibling-module
    dependencies must be installed or it can resolve stale jars from ~/.m2.
    ID (the NonDex goal) and NIO (``mvn test``) compile on their own.
    """
    if test_type not in ("od", "td"):
        return {
            "ok": True, "status": "SKIPPED", "returncode": 0,
            "timed_out": False, "duration_seconds": 0.0,
            "container_workdir": container_workdir,
        }
    pre = "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT\n" if test_type == "od" else ""
    clean_cmd = (
        "find . -type d -name target -prune -exec rm -rf '{}' +\n"
        if clean else "")
    if test_type == "td":
        lifecycle = "install -DskipTests"
        also_make = " -am"
    else:
        lifecycle = "test-compile"
        also_make = ""
    cmd = (
        f"set -o pipefail\n{pre}"
        f"cd {shlex.quote(container_workdir)} || exit 97\n"
        f"{clean_cmd}"
        f"timeout -k 30s {COMPILE_TIMEOUT_S}s "
        f"mvn -B -ntp {lifecycle} -pl {shlex.quote(module)}{also_make} "
        f"{MVNOPTS_OD} 2>&1"
    )
    log(f"compiling test classes in {container_workdir}"
        + (" from a clean target state" if clean else ""))
    started = time.monotonic()
    timed_out = False
    try:
        proc = run(
            ["docker", "exec", "-u", "0", docker_container, "bash", "-c", cmd],
            check=False, capture_output=True, text=True,
            timeout=COMPILE_TIMEOUT_S + 120)
        output = (proc.stdout or "") + (proc.stderr or "")
        rc = proc.returncode
        timed_out = rc in {124, 137}
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        rc = 124
        out = exc.stdout or ""
        err = exc.stderr or ""
        if isinstance(out, bytes):
            out = out.decode("utf-8", "replace")
        if isinstance(err, bytes):
            err = err.decode("utf-8", "replace")
        output = out + err + "\n[agentflake] compile timed out\n"
    duration = time.monotonic() - started
    quiesce_error = ""
    try:
        # A candidate-controlled pom/plugin can outlive Maven by spawning a
        # detached process.  Kill and verify that process family before any
        # protected oracle material is prepared or another context is exposed.
        quiesce_agent_processes(docker_container)
    except RuntimeError as exc:
        quiesce_error = str(exc)
        output += ("\n[agentflake] post-compile process isolation failed: "
                   + quiesce_error + "\n")
    reclaim_error = _reclaim_container_path(
        docker_container, container_workdir)
    if reclaim_error:
        output += ("\n[agentflake] could not reclaim compiled worktree: "
                   + reclaim_error + "\n")
    if log_path is not None:
        Path(log_path).parent.mkdir(parents=True, exist_ok=True)
        Path(log_path).write_text(output, encoding="utf-8")
    return {
        "ok": (rc == 0 and not timed_out and not reclaim_error
               and not quiesce_error),
        "status": (
            "PASSED" if (rc == 0 and not timed_out and not reclaim_error
                         and not quiesce_error)
            else "FAILED"),
        "returncode": rc,
        "timed_out": timed_out,
        "duration_seconds": round(duration, 3),
        "container_workdir": container_workdir,
        "command": cmd,
        "log": str(log_path) if log_path is not None else None,
        "output_tail": output[-2000:],
        "reclaim_error": reclaim_error,
        "quiesce_error": quiesce_error,
    }


def parse_stream(ndjson_path: Path, steps: Path, wall_ms: int = 0):
    """Split the `codex exec --json` stream into thinking / tool-call / usage.

    Event envelope (codex-rs/exec/src/exec_events.rs):
        {"type": "thread.started",  "thread_id": "..."}
        {"type": "turn.started"}
        {"type": "turn.completed",  "usage": {...}}
        {"type": "turn.failed",     "error": {"message": "..."}}
        {"type": "item.started"|"item.updated"|"item.completed", "item": {...}}
        {"type": "error",           "message": "..."}

    Items carry a stable ``id`` and are re-emitted as they progress, so they are
    collapsed by id (last write wins) rather than appended per event — otherwise
    a single command would be counted once per lifecycle event. Items that only
    ever reached ``item.started`` are still kept: when the agent is killed by
    the timeout, the in-flight command is exactly the evidence worth having.
    """
    TOOL_TYPES = {"command_execution", "file_change", "mcp_tool_call",
                  "web_search", "dynamic_tool_call"}

    items: dict = {}        # item id -> latest payload
    order: list = []        # first-seen order of item ids
    usage = {
        "input_tokens": 0,
        "cached_input_tokens": 0,
        "cache_write_input_tokens": 0,
        "output_tokens": 0,
        "reasoning_output_tokens": 0,
    }
    num_turns = 0
    is_error = False
    errors: list = []
    thread_id = None

    if ndjson_path.is_file():
        for line in ndjson_path.read_text(
                encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            rtype = rec.get("type")

            if rtype == "thread.started":
                thread_id = rec.get("thread_id") or thread_id

            elif rtype == "turn.completed":
                num_turns += 1
                blob = rec.get("usage") or {}
                if isinstance(blob, dict):
                    for key in usage:
                        try:
                            usage[key] += int(blob.get(key) or 0)
                        except (TypeError, ValueError):
                            pass

            elif rtype == "turn.failed":
                is_error = True
                err = rec.get("error") or {}
                errors.append(str(err.get("message") or err) if isinstance(err, dict)
                              else str(err))

            elif rtype == "error":
                is_error = True
                errors.append(str(rec.get("message") or "unspecified codex error"))

            elif rtype in ("item.started", "item.updated", "item.completed"):
                item = rec.get("item") or {}
                if not isinstance(item, dict):
                    continue
                # Fall back to a positional key if an item ever lacks an id, so
                # unidentified items are still recorded instead of colliding.
                key = item.get("id") or f"_anon_{len(order)}"
                if key not in items:
                    order.append(key)
                items[key] = item

    thinking: list = []
    tool_calls: list = []
    for key in order:
        item = items.get(key) or {}
        itype = item.get("type")
        if itype == "reasoning":
            text = item.get("text")
            if text:
                thinking.append(text)
        elif itype in TOOL_TYPES:
            if itype == "command_execution":
                payload = {
                    "command": item.get("command"),
                    "status": item.get("status"),
                    "exit_code": item.get("exit_code"),
                }
            elif itype == "file_change":
                payload = {
                    "changes": item.get("changes"),
                    "status": item.get("status"),
                }
            elif itype == "mcp_tool_call":
                payload = {
                    "server": item.get("server"),
                    "tool": item.get("tool"),
                    "arguments": item.get("arguments"),
                    "status": item.get("status"),
                }
            else:
                payload = {k: v for k, v in item.items()
                           if k not in ("id", "type")}
            tool_calls.append({"name": itype, "input": payload})

    usage["total_tokens"] = usage["input_tokens"] + usage["output_tokens"]
    usage_doc = {
        "usage": usage,
        "num_turns": num_turns,
        # Codex does not report a turn duration, so this is the driver's
        # wall-clock measurement around the `docker exec`.
        "duration_ms": wall_ms,
        "is_error": is_error,
        "error": "; ".join(errors) if errors else None,
        "thread_id": thread_id,
        "subtype": "codex-exec",
    }

    (steps / "thinking.txt").write_text(
        "\n\n".join(thinking), encoding="utf-8")
    with (steps / "tool_calls.jsonl").open("w", encoding="utf-8") as f:
        for tc in tool_calls:
            f.write(json.dumps(tc) + "\n")
    (steps / "usage.json").write_text(
        json.dumps(usage_doc, indent=2), encoding="utf-8")
    return len(thinking), len(tool_calls), usage_doc


def _oracle_outcome_dict(outcome) -> dict:
    return {
        "disposition": outcome.disposition.value,
        "code": outcome.code,
        "message": outcome.message,
        "details": dict(outcome.details),
    }


def _selected_verify_record(document: dict) -> dict:
    attempts = document.get("attempts") or []
    if not isinstance(attempts, list):
        return {}
    selected = document.get(
        "selected_command_attempt", document.get("selected_attempt"))
    for item in attempts:
        if isinstance(item, dict) and item.get("attempt") == selected:
            return item
    dictionaries = [item for item in attempts if isinstance(item, dict)]
    return dictionaries[-1] if dictionaries else {}


def _verify_observation(document: dict) -> RunObservation:
    """Translate strict verifier evidence into the oracle calibration model."""
    record = _selected_verify_record(document)
    stats = record.get("stats") or {}
    tests = int(stats.get("tests") or 0)
    failures = int(stats.get("failures") or 0)
    errors = int(stats.get("errors") or 0)
    skipped = int(stats.get("skipped") or 0)
    markers = int(stats.get("failure_markers") or stats.get("markers") or 0)
    rc = stats.get("returncode", stats.get("rc"))
    build_success = bool(stats.get("build_success"))
    detail = str(
        document.get("validation_reason")
        or document.get("final_reason")
        or record.get("reason")
        or "")
    if failures + errors + markers > 0 and tests > 0:
        return RunObservation(
            TestRunOutcome.TEST_FAILURE, rc, tests, failures, errors,
            skipped, build_success, detail)
    if document.get("final_verdict") == "PASSED":
        return RunObservation(
            TestRunOutcome.PASSED, rc, tests, failures, errors,
            skipped, build_success, detail)
    return RunObservation.infra(detail or "strict verifier produced no valid result", rc)


def _calibration_dict(result) -> dict:
    records = []
    for record in result.records:
        observed = record.observed
        records.append({
            "tree": record.tree,
            "run": record.run,
            "expected": (
                record.expected.value
                if record.expected is not None else "SOUND_EXECUTION"),
            "observed": {
                "outcome": observed.outcome.value,
                "returncode": observed.returncode,
                "tests": observed.tests,
                "failures": observed.failures,
                "errors": observed.errors,
                "skipped": observed.skipped,
                "build_success": observed.build_success,
                "detail": observed.detail,
            },
        })
    return {
        "outcome": _oracle_outcome_dict(result.outcome),
        "records": records,
    }


def _sha256_path(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_write_text(path: Path, content: str) -> None:
    """Replace a terminal artifact atomically without following its old link."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    if temporary.is_symlink() or temporary.is_file():
        temporary.unlink()
    elif temporary.exists():
        shutil.rmtree(temporary)
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def _find_source_in_tree(root: Path, module: str, relative: str) -> Path | None:
    roots = []
    if module and module != ".":
        roots.append(root / module)
    roots.append(root)
    for project in roots:
        for source_dir in ("src/test/java", "src/main/java"):
            candidate = project / source_dir / relative
            if candidate.is_file():
                return candidate
    suffix = Path(relative).as_posix()
    hits = sorted(
        path for path in root.rglob(Path(relative).name)
        if path.is_file()
        and path.as_posix().endswith("/src/test/java/" + suffix)
    )
    return hits[0] if hits else None


def _extract_local_method_closure(path: Path, method: str) -> str | None:
    """Extract the victim plus same-file helpers it calls, transitively."""
    pending = [method]
    seen = set()
    blocks = []
    call_re = re.compile(r"\b([A-Za-z_$][\w$]*)\s*\(")
    keywords = {
        "if", "for", "while", "switch", "catch", "new", "return",
        "throw", "synchronized", "assert",
    }

    def _declared_blocks(name: str):
        source_text = path.read_text(
            encoding="utf-8", errors="replace")
        source_lines = source_text.splitlines(keepends=True)
        masked_lines = _code_mask(source_text).splitlines(keepends=True)
        if len(masked_lines) != len(source_lines):
            masked_lines = source_lines
        declaration_lines = []
        name_re = re.compile(r"\b" + re.escape(name) + r"\s*\(")
        for line_number, line in enumerate(masked_lines, start=1):
            match = name_re.search(line)
            if not match:
                continue
            prefix = line[:match.start()].rstrip()
            if not prefix or not re.search(r"[\w$>\]]$", prefix):
                continue
            if prefix.split()[-1] in {
                    "return", "new", "throw", "yield", "assert", "if",
                    "while", "for", "switch", "catch"}:
                continue
            declaration_lines.append(line_number)
        extracted = []
        for line_number in declaration_lines:
            start = line_number - 1
            depth = 0
            found_open = False
            end = None
            for index in range(start, len(masked_lines)):
                for char in masked_lines[index]:
                    if char == "{":
                        depth += 1
                        found_open = True
                    elif char == "}":
                        depth -= 1
                        if found_open and depth == 0:
                            end = index
                            break
                if end is not None:
                    break
            block = (
                "".join(source_lines[start:end + 1])
                if end is not None else
                extract_java_method(
                    str(path), name, target_line=line_number)
            )
            if block and block not in extracted:
                extracted.append(block)
        return extracted

    while pending and len(seen) < 64:
        name = pending.pop(0)
        if name in seen or name in keywords:
            continue
        seen.add(name)
        declared = _declared_blocks(name)
        if not declared:
            continue
        for block in declared:
            blocks.append(f"// method-closure: {name}\n{block}")
            for called in call_re.findall(block):
                if called not in seen and called not in keywords:
                    pending.append(called)
    return "\n".join(blocks) if blocks else None


def _td_audit_victim_oracle(row: dict, module: str, pristine: Path,
                            fixed: Path, candidate: Path) -> dict:
    """Reject obvious victim-oracle weakening using B/F as protected controls.

    This is method-specific. The old class-wide "some assertion remains"
    heuristic allowed a candidate to delete every assertion in the victim while
    leaving an unrelated assertion elsewhere in the class. Stable assertions
    shared by both pristine and developer-fixed controls must survive, and a
    candidate may not add common skip/suppression constructs.
    """
    victim = (row.get("flaky_test") or "").strip()
    try:
        relative, method = fqn_to_path(victim)
    except Exception as exc:
        return {
            "ok": False,
            "reason_code": "VICTIM_FQN_UNRESOLVED_FAIL_CLOSED",
            "error": repr(exc),
        }
    files = {
        "pristine": _find_source_in_tree(pristine, module, relative),
        "fixed": _find_source_in_tree(fixed, module, relative),
        "candidate": _find_source_in_tree(candidate, module, relative),
    }
    missing = [name for name, path in files.items() if path is None]
    if missing:
        return {
            "ok": False,
            "reason_code": "VICTIM_SOURCE_MISSING_FAIL_CLOSED",
            "missing": missing,
            "relative_path": relative,
        }

    sources = {
        name: path.read_text(encoding="utf-8", errors="replace")
        for name, path in files.items()
    }
    if method:
        methods = {
            name: _extract_local_method_closure(path, method)
            for name, path in files.items()
        }
    else:
        methods = dict(sources)
    missing_methods = [name for name, text in methods.items() if not text]
    if missing_methods:
        return {
            "ok": False,
            "reason_code": "VICTIM_METHOD_MISSING_FAIL_CLOSED",
            "missing": missing_methods,
            "relative_path": relative,
            "method": method,
        }

    suppression_patterns = {
        "ignored_annotation": r"@(?:Ignore|Disabled)\b",
        "disabled_test": r"@Test\s*\([^)]*\benabled\s*=\s*false",
        "assumption": r"\b(?:Assume\.)?assume(?:True|False|That|NoException)\s*\(",
        "catch_assertion": r"catch\s*\(\s*(?:Throwable|Error|AssertionError)\b",
        "constant_assertion": r"\b(?:assertTrue\s*\(\s*true|assertFalse\s*\(\s*false)\b",
    }
    added_suppressions = []
    for label, pattern in suppression_patterns.items():
        candidate_hits = len(re.findall(pattern, sources["candidate"]))
        control_hits = max(
            len(re.findall(pattern, sources["pristine"])),
            len(re.findall(pattern, sources["fixed"])),
        )
        if candidate_hits > control_hits:
            added_suppressions.append({
                "kind": label,
                "candidate": candidate_hits,
                "control_max": control_hits,
            })

    # Method-local reachability guards must be inspected separately from class
    # annotations.  Exact assertion-line preservation alone is insufficient:
    # `if (false) { assert...; }` keeps the same Counter entry while deleting
    # the oracle at runtime.
    reachability_patterns = {
        "constant_false_branch": r"\b(?:if|while)\s*\(\s*false\s*\)",
    }
    masked_methods = {
        name: _code_mask(text) for name, text in methods.items()
    }
    for label, pattern in reachability_patterns.items():
        candidate_hits = len(re.findall(pattern, masked_methods["candidate"]))
        control_hits = max(
            len(re.findall(pattern, masked_methods["pristine"])),
            len(re.findall(pattern, masked_methods["fixed"])),
        )
        if candidate_hits > control_hits:
            added_suppressions.append({
                "kind": label,
                "candidate": candidate_hits,
                "control_max": control_hits,
            })

    assertion_re = re.compile(
        r"\b(?:assert\w*|fail|verify)\s*\(", re.IGNORECASE)
    assertion_call_re = re.compile(
        r"\b(assert\w*|fail|verify)\s*\((.*)\)", re.IGNORECASE)
    leading_message_re = re.compile(
        r'^\s*"(?:\\.|[^"\\])*"\s*,\s*')

    def _assertions(text):
        signatures = []
        for line in text.splitlines():
            if not assertion_re.search(line):
                continue
            match = assertion_call_re.search(line)
            if not match:
                signatures.append(" ".join(line.strip().split()))
                continue
            function, arguments = match.group(1).lower(), match.group(2)
            # Adding an assertion description does not weaken its predicate.
            # Strip only a literal leading message; expected/actual expressions
            # remain exact and protected.
            if function != "fail":
                arguments = leading_message_re.sub("", arguments, count=1)
            signatures.append(
                function + "(" + "".join(arguments.split()) + ")")
        return Counter(signatures)

    assertions = {name: _assertions(text) for name, text in methods.items()}
    stable = assertions["pristine"] & assertions["fixed"]
    missing_stable = sorted(
        (stable - assertions["candidate"]).elements())
    minimum_assertions = min(
        sum(assertions["pristine"].values()),
        sum(assertions["fixed"].values()))
    candidate_assertions = sum(assertions["candidate"].values())
    statement_counts = {
        name: text.count(";") for name, text in methods.items()
    }
    minimum_statements = min(
        statement_counts["pristine"], statement_counts["fixed"])
    body_gutted = (
        minimum_statements >= 4
        and statement_counts["candidate"] < max(2, minimum_statements // 2)
    )
    ok = (
        not added_suppressions
        and not missing_stable
        and candidate_assertions >= minimum_assertions
        and not body_gutted
    )
    return {
        "ok": ok,
        "reason_code": (
            "VICTIM_ORACLE_INTEGRITY_VERIFIED"
            if ok else "VICTIM_ORACLE_WEAKENED_FAIL_CLOSED"),
        "relative_path": relative,
        "method": method,
        "stable_assertions": sum(stable.values()),
        "missing_stable_assertions": missing_stable,
        "assertion_counts": {
            name: sum(value.values()) for name, value in assertions.items()
        },
        "statement_counts": statement_counts,
        "minimum_allowed_assertions": minimum_assertions,
        "added_suppressions": added_suppressions,
        "body_gutted": body_gutted,
    }




def current_run_label() -> str:
    return (os.environ.get("AGENTIC_RUN_LABEL") or "run_01").strip()


def container_run_dir(container: str) -> Path:
    return Path(DATA_DIR) / container / current_run_label()


def main():
    global VERIFY_PASS_RUNS, AGENT_TIMEOUT_S

    ap = argparse.ArgumentParser()
    ap.add_argument("container")
    ap.add_argument("--docker-container")
    ap.add_argument("--model", default=None,
                    help="model id passed to `codex exec --model` "
                         "(default: agentic_config.DEFAULT_MODEL)")
    ap.add_argument("--reasoning-effort", default=None,
                    choices=["minimal", "low", "medium", "high"],
                    help="forwarded as -c model_reasoning_effort "
                         "(default: agentic_config.MODEL_REASONING_EFFORT)")
    ap.add_argument("--max-iterations", type=int, default=None,
                    help="accepted for compatibility; codex exec has no "
                         "turn cap, so this is logged and ignored")
    ap.add_argument("--verify-pass-runs", type=int, default=None,
                    help=f"extra passing verification runs required after the "
                         f"first pass (default: {VERIFY_PASS_RUNS})")
    ap.add_argument("--cli-timeout-s", type=int, default=None,
                    help=f"wall-clock cap in seconds for Codex "
                         f"(default: {AGENT_TIMEOUT_S})")
    args = ap.parse_args()

    # Model / reasoning-effort defaults come from agentic_config so a single
    # edit there changes every entry point.
    if not args.model:
        try:
            from agentic_config import DEFAULT_MODEL as _DEF_MODEL  # type: ignore  # noqa: E402
        except Exception:
            _DEF_MODEL = "gpt-5.4"
        args.model = os.environ.get("AGENTIC_MODEL", "").strip() or _DEF_MODEL
    if not args.reasoning_effort:
        try:
            from agentic_config import MODEL_REASONING_EFFORT as _DEF_EFFORT  # type: ignore  # noqa: E402
        except Exception:
            _DEF_EFFORT = "high"
        args.reasoning_effort = (
            os.environ.get("AGENTIC_REASONING_EFFORT", "").strip() or _DEF_EFFORT)

    if args.verify_pass_runs is not None:
        VERIFY_PASS_RUNS = args.verify_pass_runs
    if args.cli_timeout_s is not None:
        AGENT_TIMEOUT_S = args.cli_timeout_s

    container = args.container
    docker_container = args.docker_container or (
        "tm_" + re.sub(r"[^a-zA-Z0-9]", "_", container))

    row = load_csv_row(container)
    if not row:
        sys.exit(f"ERROR: container '{container}' not in test_config.csv")
    test_type = (row.get("test_type") or "").strip().lower()
    if test_type not in SUPPORTED_TYPES:
        sys.exit(f"ERROR: agentic_codex_cli supports {sorted(SUPPORTED_TYPES)} "
                 f"only (got '{test_type}').")
    module = (row.get("module") or ".").strip()

    base = container_run_dir(container)
    flaky = base / "Flaky"
    inputs = base / "codex_inputs"
    steps = base / "codex_outputs"
    inputs.mkdir(parents=True, exist_ok=True)
    steps.mkdir(parents=True, exist_ok=True)
    input_rel = "codex_inputs"
    output_rel = "codex_outputs"

    log_dir = FAILURE_LOG_DIR.get(test_type, "traces-flaky")
    for p in (flaky, base / log_dir / "mvn.log"):
        if not p.exists():
            sys.exit(f"ERROR: expected '{p}' (the launcher must run first)")

    # External workspace OUTSIDE the /app/work bind mount. The agent runs with
    # unrestricted filesystem access inside /app/work, so anything kept there
    # (a Flaky-local .git, the launcher's Flaky.pristine) can be deleted by the
    # agent. Keeping
    # the git metadata and a baseline copy here makes capture + restore robust
    # no matter what the agent does to /app/work.
    ext = Path(tempfile.mkdtemp(prefix=f"agentcli_{container}_"))
    ext_gitdir = ext / "flaky.git"
    ext_baseline = ext / "baseline"
    # Guarantee the external workspace (a full source-tree copy) is removed on
    # EVERY exit path — exception, sys.exit, or Ctrl-C — not just the success
    # path below. Otherwise a batch run leaks a project copy into /tmp per crash.
    atexit.register(lambda: shutil.rmtree(ext, ignore_errors=True))

    # ---- TD: install the forced-reproduction harness the prompt points at ---
    # Must exist before the prompt is written (it names the script) and before
    # the agent runs. Fail closed: handing the agent a reproduction command that
    # can never show the failure wastes the whole turn budget on a green test.
    if test_type == "td":
        if install_td_repro_helper(
                base, inputs, module, (row.get("flaky_test") or "").strip()):
            log(f"installed TD repro harness at "
                f"/app/work/codex_inputs/{TD_REPRO_REL} "
                "(disposable clean compile + forced victim run)")
        else:
            sys.exit("ERROR: could not install the TD repro harness — "
                     "FlakyCodeChange.patch is missing or has no source hunks. "
                     "The agent would be handed a reproduction command that can "
                     "never reproduce the failure; refusing to burn a run.")

    # ---- assemble prompts --------------------------------------------------
    log("assembling prompts")
    user_prompt, system_prompt = assemble_prompts(row, base)
    (inputs / "prompt_user.txt").write_text(user_prompt, encoding="utf-8")
    (inputs / "prompt_system.txt").write_text(system_prompt, encoding="utf-8")

    # ---- protected baseline (external git-dir + copy) ----------------------
    log(f"snapshotting protected baseline at {ext}")
    shutil.rmtree(flaky / ".git", ignore_errors=True)
    (flaky / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
    shutil.copytree(flaky, ext_baseline)
    git(flaky, "init", "-q", gitdir=ext_gitdir)
    git(flaky, "add", "-A", gitdir=ext_gitdir)
    git(flaky, "commit", "-q", "-m", "baseline", gitdir=ext_gitdir)

    # ---- run the agent -----------------------------------------------------
    max_turns = args.max_iterations or os.environ.get("AGENTIC_MAX_ITERATIONS")
    simulated = (os.environ.get(SIMULATED_AGENT_ENV) or "").strip()
    if simulated:
        log(f"SIMULATED AGENT MODE ({SIMULATED_AGENT_ENV}={simulated}) — no API "
            f"call; everything after the agent runs for real")
        agent_rc = run_simulated_agent(
            docker_container, base, output_rel, Path(simulated))
        agent_wall_ms = 0
    else:
        agent_rc, agent_wall_ms = run_agent_in_container(
            docker_container, args.model, args.reasoning_effort, max_turns,
            input_rel, output_rel)
    log(f"agent exit code: {agent_rc}")
    try:
        quiesce_agent_processes(docker_container)
    except RuntimeError as exc:
        sys.exit(f"ERROR: {exc} — refusing to capture or validate a tree that "
                 "may still be changing.")

    # codex_outputs was writable during the agent turn.  Remove every
    # evaluator-owned terminal artifact before trusting or producing evidence;
    # otherwise an agent-precreated PASSED file could survive a later driver
    # crash and be mistaken for a completed evaluation.
    reclaim_error = _reclaim_container_path(
        docker_container, "/app/work/codex_outputs")
    if reclaim_error:
        sys.exit("ERROR: could not reclaim evaluator output paths after the "
                 f"agent turn: {reclaim_error}")
    for artifact_name in (
            "run_verdict.txt", "verify_after_fix.verdict",
            "verify_after_fix.result.json", "verify_after_fix.log",
            "meta.json"):
        artifact = steps / artifact_name
        if artifact.is_symlink() or artifact.is_file():
            artifact.unlink()
        elif artifact.exists():
            shutil.rmtree(artifact)

    # ---- capture the patch (external git-dir; never the outer repo) --------
    # Hardened: a swallowed git failure here would emit an EMPTY patch, which is
    # re-applied as a no-op and scored as a FAILED repair — masking an infra
    # problem (root-owned files the agent wrote into the bind mount, a stale
    # index lock, etc.) as "the model couldn't fix it". Surface it instead of
    # silently producing an empty diff.
    # The baseline .gitignore is evaluator-owned. Restore it after the agent so
    # the agent cannot hide a newly added source file by adding an ignore rule.
    (flaky / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
    add = git(flaky, "add", "-A", gitdir=ext_gitdir, check=False)
    if add.returncode != 0:
        sys.exit(f"ERROR: git add -A failed while capturing the agent's patch "
                 f"(rc={add.returncode}): {(add.stderr or '').strip()} — "
                 f"refusing to emit an empty patch that would be misscored as a "
                 f"FAILED repair.")
    res = git(flaky, "diff", "--cached", "HEAD", gitdir=ext_gitdir, check=False)
    if res.returncode != 0:
        sys.exit(f"ERROR: git diff --cached HEAD failed while capturing the "
                 f"agent's patch (rc={res.returncode}): {(res.stderr or '').strip()}")
    diff = res.stdout
    # Strip binary sections BEFORE the empty check: one stray .DS_Store or build
    # artifact would otherwise make `git apply` reject the whole patch and lose a
    # real source fix (see strip_binary_hunks).
    diff, dropped_binary = strip_binary_hunks(diff)
    if dropped_binary:
        log(f"dropped {len(dropped_binary)} binary section(s) from the captured "
            f"patch — they would abort `git apply` and discard the source fix: "
            f"{dropped_binary[:5]}")
    if not diff.strip():
        # Clean add/diff but no net change: a genuine no-fix outcome (correctly
        # scored FAILED). Make it observable rather than silently empty.
        log("NOTE: agent produced no net file changes — empty patch "
            "(genuine no-fix outcome; will be scored FAILED).")
    (steps / "patch.diff").write_text(diff, encoding="utf-8")
    log(f"captured patch.diff ({len(diff)} bytes)")

    # ---- write llm_response.json in the shape apply_fix.py expects ---------
    (steps / "llm_response.json").write_text(json.dumps({
        "response": {
            "output_a": {"patch": diff},
            "output_b": {"fixed_code": []},
        }
    }, indent=2), encoding="utf-8")

    # One verify helper, defined here so calibration, the ID discrimination
    # gate, and post-fix verification share exactly the same strict parser.
    verdict_path = steps / "verify_after_fix.verdict"
    result_path = steps / "verify_after_fix.result.json"
    validation_dir = steps / "td_validation"
    # codex_outputs is writable during the agent turn. Do not trust a
    # pre-created td_validation symlink/directory as the destination for
    # protected oracle material or authoritative evidence.
    if validation_dir.is_symlink():
        validation_dir.unlink()
    elif validation_dir.exists():
        shutil.rmtree(validation_dir)
    validation_dir.mkdir(parents=True, exist_ok=True)

    def _verify_once(*, container_workdir: str = "/app/work/Flaky",
                     validation_attempt: int | None = None,
                     phase: str = "official") -> dict:
        # Clear canonical artifacts so a crash before writing cannot leave a
        # stale PASS from an earlier control or confirmation run.
        for canonical in (verdict_path, result_path):
            if canonical.is_symlink() or canonical.is_file():
                canonical.unlink()
            elif canonical.exists():
                shutil.rmtree(canonical)
        command = [
            sys.executable, str(AGENTIC_VERIFY), container,
            "--docker-container", docker_container,
            "--container-workdir", container_workdir,
        ]
        if validation_attempt is not None:
            command += ["--attempt", str(validation_attempt)]
        rc = run(command, check=False).returncode
        quiesce_error = ""
        try:
            # Test/build code is candidate-controlled.  A detached child must
            # not survive one attempt and observe or mutate the next context.
            quiesce_agent_processes(docker_container)
        except RuntimeError as exc:
            quiesce_error = str(exc)
        if quiesce_error:
            document = {
                "schema_version": 1,
                "final_verdict": "FAILED",
                "validation_status": "INCOMPLETE",
                "final_reason": "POST_VERIFY_PROCESS_ISOLATION_FAIL_CLOSED",
                "error": quiesce_error,
                "verifier_returncode": rc,
                "attempts": [],
            }
        elif result_path.is_file():
            try:
                document = json.loads(result_path.read_text(encoding="utf-8"))
                if not isinstance(document, dict):
                    raise ValueError("verifier result root is not an object")
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                document = {
                    "schema_version": 1,
                    "final_verdict": "FAILED",
                    "validation_status": "INCOMPLETE",
                    "final_reason": "malformed_verifier_result_FAIL_CLOSED",
                    "error": repr(exc),
                    "attempts": [],
                }
        else:
            document = {
                "schema_version": 1,
                "final_verdict": "FAILED",
                "validation_status": "INCOMPLETE",
                "final_reason": "verifier_no_result_FAIL_CLOSED",
                "verifier_returncode": rc,
                "attempts": [],
            }
        # A PASS is accepted only when the verifier process completed and its
        # structured evidence is internally consistent.  This catches the
        # narrow failure window where the verifier wrote a canonical PASS but
        # then failed to create its immutable archive, plus malformed/forged
        # result combinations such as PASS + internal INCOMPLETE.
        if document.get("final_verdict") == "PASSED":
            evidence_errors = []
            status = (
                document.get("final_validation_status")
                or document.get("validation_status"))
            record = _selected_verify_record(document)
            stats = record.get("stats") or {}
            if rc != 0:
                evidence_errors.append(f"verifier_returncode={rc}")
            if document.get("schema_version") != 1:
                evidence_errors.append("unsupported_schema")
            if status != "PASSED":
                evidence_errors.append(f"internal_status={status!r}")
            if document.get("container") != container:
                evidence_errors.append("container_mismatch")
            if document.get("docker_container") != docker_container:
                evidence_errors.append("docker_container_mismatch")
            if document.get("test_type") != test_type:
                evidence_errors.append("test_type_mismatch")
            if document.get("container_workdir") != container_workdir:
                evidence_errors.append("workdir_mismatch")
            if document.get("validation_attempt") != validation_attempt:
                evidence_errors.append("validation_attempt_mismatch")
            if (record.get("validation_status") != "PASSED"
                    or stats.get("returncode") != 0
                    or bool(stats.get("timed_out"))
                    or int(stats.get("tests") or 0) < 1
                    or int(stats.get("failures") or 0) != 0
                    or int(stats.get("errors") or 0) != 0
                    or int(stats.get("skipped") or 0) != 0
                    or int(stats.get("failure_markers") or 0) != 0
                    or not bool(stats.get("build_success"))):
                evidence_errors.append("selected_attempt_not_strict_pass")
            if validation_attempt is not None:
                archive_dir = validation_dir / "runs"
                archive_json = (
                    archive_dir / f"attempt_{validation_attempt:02d}.json")
                archive_log = (
                    archive_dir / f"attempt_{validation_attempt:02d}.log")
                try:
                    archived = json.loads(
                        archive_json.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    archived = None
                    evidence_errors.append(
                        f"immutable_archive_unreadable={type(exc).__name__}")
                if archived is not None and archived != document:
                    evidence_errors.append("immutable_archive_mismatch")
                if not archive_log.is_file():
                    evidence_errors.append("immutable_archive_log_missing")
            if evidence_errors:
                document = {
                    "schema_version": 1,
                    "final_verdict": "FAILED",
                    "validation_status": "INCOMPLETE",
                    "final_reason": (
                        "VERIFIER_EVIDENCE_INCONSISTENT_FAIL_CLOSED"),
                    "verifier_returncode": rc,
                    "evidence_errors": evidence_errors,
                    "attempts": [],
                }
        # The externally visible contract is deliberately binary. Structured
        # evidence retains the internal validation status/reason, but anything
        # short of a strict PASS fails closed.
        document.setdefault(
            "validation_status",
            document.get("final_validation_status")
            or document.get("final_verdict", "INCOMPLETE"))
        if document.get("final_verdict") != "PASSED":
            document["final_verdict"] = "FAILED"
        document["phase"] = phase
        document["container_workdir"] = container_workdir
        _atomic_write_text(verdict_path, document["final_verdict"] + "\n")
        _atomic_write_text(
            result_path, json.dumps(document, indent=2) + "\n")
        return document

    # ---- restore the protected baseline, then re-apply via the applier -----
    log("restoring Flaky/ from the protected baseline")
    try:
        _replace_tree_from(ext_baseline, flaky,
                           docker_container=docker_container,
                           container_dest="/app/work/Flaky",
                           label="protected baseline restore")
    except RestoreTreeError as exc:
        sys.exit(f"ERROR: {exc}")
    # A Flaky-local .git so apply_fix's `git apply` uses THIS tree (the outer
    # Valg repo gitignores data/**/Flaky, which makes git apply silently skip).
    # Safe now — the agent is no longer running. GIT_CEILING (in git()) keeps
    # it from escaping upward even if this .git is somehow absent.
    git(flaky, "init", "-q")
    git(flaky, "add", "-A")
    git(flaky, "commit", "-q", "-m", "baseline")

    # ---- ID discrimination gate (fail-closed) ------------------------------
    # NonDex ID flakiness is probabilistic and, for some subjects, not even
    # reproducible at a fixed seed, so a single post-fix verify can PASS on
    # UNFIXED code -> an empty/no-op patch is then scored PASSED (the observed
    # shardingsphere false-PASS). Guard: the SAME verify must FAIL the UNFIXED
    # victim at least once here — on the pristine tree, BEFORE apply_fix lands the
    # patch — otherwise the verify cannot tell a fix from no-fix and any later
    # PASS is meaningless, so we fail closed. Mirrors the TD forced-verify gate.
    # Discriminative subjects (e.g. a fixed-seed order reversal) fail on run 1 and
    # cost a single extra verify; only non-reproducing ones spend the full budget.
    id_discriminative = True
    if test_type == "id":
        log(f"ID gate: does the UNFIXED victim fail the verify? "
            f"(up to {VERIFY_PASS_RUNS} run(s), on the pristine tree)")
        id_discriminative = False
        for i in range(1, VERIFY_PASS_RUNS + 1):
            bv = _verify_once(phase=f"id_discrimination_{i}")["final_verdict"]
            log(f"  gate {i}/{VERIFY_PASS_RUNS}: unfixed victim -> {bv}")
            if bv == "FAILED":
                id_discriminative = True
                log(f"  unfixed victim failed on run {i} -> verify is "
                    f"discriminative; proceeding to apply + verify the fix.")
                break
        if not id_discriminative:
            log(f"  unfixed victim PASSED all {VERIFY_PASS_RUNS} runs -> the NonDex "
                f"verify cannot reproduce this container's flakiness; a fix PASS "
                f"would be meaningless. Fail-closed (verdict FAILED).")

    log("apply_fix.py")
    run([sys.executable, str(APPLY_FIX), container,
         "--docker-container", docker_container], check=False)
    # The worktree-local repository has served its only purpose: directing
    # apply_fix.py's `git apply` at Flaky/ instead of the outer repository.
    # Remove it before Docker compilation so ownership reclamation only covers
    # candidate/build files and cannot fail on host-created Git pack objects.
    try:
        _remove_evaluator_git_metadata(flaky)
    except RestoreTreeError as exc:
        sys.exit(f"ERROR: {exc}")

    # ---- NIO oracle integrity: restore the pristine generated wrapper --------
    # The NIO verify oracle is a generated wrapper class (#runTwice) that lives
    # in the AGENT-EDITABLE tree, and the captured+re-applied patch.diff can
    # carry an agent edit to it (GITIGNORE_BODY does not exclude the wrapper
    # path). A weakened #runTwice (dropped 2nd-run assert, try/catch, no-op)
    # would be scored a false PASSED. apply_fix has now landed the patch, so the
    # agent's legitimate VICTIM fix is on disk; we overwrite ONLY the wrapper
    # file with pristine source. Verify ("mvn test ... #runTwice") re-runs
    # test-compile from this on-disk source, so the executed oracle is the
    # unmodified one. Last writer before compile/verify => dominates any tamper,
    # independent of how the patch encoded it. Gated on NIO; od/td/id untouched.
    if test_type == "nio":
        wrapper_fqcn = (os.environ.get("WRAPPER_FQCN") or "").strip()
        if not wrapper_fqcn:
            try:  # fallback: persisted by run_agentic_nio.sh
                tc = json.loads((inputs / "trace_config.json").read_text(
                    encoding="utf-8"))
                wrapper_fqcn = (tc.get("wrapper_fqcn") or "").strip()
            except Exception:
                wrapper_fqcn = ""
        if not wrapper_fqcn:
            sys.exit("ERROR: NIO run missing WRAPPER_FQCN (env and "
                     "trace_config.json); refusing to verify a possibly-"
                     "tampered oracle.")

        pkg, _, simple = wrapper_fqcn.rpartition(".")  # pkg="" for default pkg
        pkg_path = pkg.replace(".", "/")
        # Mirror run_agentic_nio.sh WRAPPER_PATH_REL: MODULE in (".","") means the
        # shell wrote "./src/..." which the FS normalizes to "src/..." — so omit
        # the module path segment in that case (build via Path(*parts), no ".").
        mod = (module or ".").strip()
        parts = [] if mod in (".", "") else [mod]
        parts += ["src", "test", "java"]
        if pkg_path:
            parts += pkg_path.split("/")
        parts += [simple + ".java"]
        rel = Path(*parts)

        live = flaky / rel
        pristine = ext_baseline / rel
        if not pristine.is_file():
            fixed_alt = base / "Fixed" / rel   # gen_wrapper wrote both trees
            if fixed_alt.is_file():
                pristine = fixed_alt
        if not pristine.is_file():
            sys.exit(f"ERROR: pristine NIO wrapper not found at {rel} in "
                     f"ext_baseline or Fixed/; cannot guarantee oracle integrity "
                     f"— refusing to score this run.")

        try:
            tampered = (not live.is_file()) or (
                live.read_bytes() != pristine.read_bytes())
        except OSError:
            tampered = True
        if tampered:
            log("SECURITY: the captured patch modified/removed the generated NIO "
                f"verify wrapper ({wrapper_fqcn}); restoring the pristine oracle "
                "before verify. The victim fix is preserved; only the wrapper is "
                "overwritten.")
        live.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(pristine, live)
        log(f"restored pristine NIO wrapper: {rel}")

        # Defense in depth: an agent could also add a SECOND class with the same
        # FQCN at a different test-source path to shadow/collide with the oracle.
        # Delete any such duplicate under <module>/src/test/java — never the
        # canonical restored file, never main sources or the victim test.
        test_root = flaky.joinpath(
            *([] if mod in (".", "") else [mod]), "src", "test", "java")
        if test_root.is_dir():
            live_resolved = live.resolve()
            pkg_decl = (re.compile(r"(?m)^\s*package\s+" + re.escape(pkg) + r"\s*;")
                        if pkg else None)
            cls_decl = re.compile(r"(?m)\bclass\s+" + re.escape(simple) + r"\b")
            for cand in test_root.rglob(simple + ".java"):
                try:
                    if cand.resolve() == live_resolved:
                        continue
                    txt = cand.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                pkg_ok = (pkg_decl.search(txt) is not None) if pkg else (
                    re.search(r"(?m)^\s*package\s+", txt) is None)
                if pkg_ok and cls_decl.search(txt):
                    log("SECURITY: removing agent-added duplicate of "
                        f"{wrapper_fqcn} at {cand.relative_to(flaky)} "
                        "(would shadow/collide with the pristine oracle).")
                    try:
                        cand.unlink()
                    except OSError:
                        pass

    # ---- TD semantic oracle ------------------------------------------------
    # A public B->P textual forcing is useful for diagnosis, but it is not a
    # faithful scorer: a patch can overfit the visible delay, and a real fix can
    # remove that patch's anchor. Official TD validation therefore uses four
    # protected controls:
    #   B  pristine, P  pristine+forcing,
    #   F  developer-fixed, FP developer-fixed+forcing.
    # We calibrate B=pass/P=fail/F=pass and require FP to execute soundly (FP
    # is an adversarial stress tree and is not guaranteed to pass). Official
    # scoring uses only the hidden F->FP context. B->P remains a
    # calibration/diagnostic context: in
    # 4/5 available TD fixtures it conflicts with the genuine reference fix,
    # so making it a candidate gate would create systematic false negatives.
    # A separate reference-on-B control proves the official context rejects a
    # no-op (or cannot compose with one) before any candidate can pass.
    validation_ready = True
    validation_reason = "READY"
    official_contexts = []
    candidate_compile = None
    forced_compile = {}
    oracle_build_payload = None
    oracle_manifest_payload = None
    calibration_payload = None
    reference_pristine_control = None
    composition_payload = None
    victim_oracle_audit = None
    oracle_work: Path | None = None
    td_execution_root: Path | None = None
    candidate_snapshot: Path | None = None

    if test_type == "td":
        candidate_snapshot = ext / "candidate"
        candidate_compile_tree = validation_dir / ".candidate_compile_work"
        try:
            shutil.copytree(
                flaky, candidate_snapshot, symlinks=True,
                ignore=_evaluation_copy_ignore(flaky))
            shutil.copytree(
                candidate_snapshot, candidate_compile_tree, symlinks=True,
                ignore=_evaluation_copy_ignore(candidate_snapshot))
            candidate_compile_workdir = (
                "/app/work/"
                + candidate_compile_tree.relative_to(base).as_posix())
            candidate_compile = compile_in_container(
                docker_container, test_type, module,
                container_workdir=candidate_compile_workdir, clean=True,
                log_path=validation_dir / "candidate_compile.log")
        except (OSError, shutil.Error) as exc:
            candidate_compile = {
                "ok": False,
                "status": "FAILED",
                "returncode": None,
                "timed_out": False,
                "reason": "candidate_snapshot_failed",
                "error": repr(exc),
            }
        finally:
            if candidate_compile_tree.exists():
                _reclaim_container_path(
                    docker_container,
                    "/app/work/"
                    + candidate_compile_tree.relative_to(base).as_posix())
                shutil.rmtree(candidate_compile_tree, ignore_errors=True)
        if not candidate_compile["ok"]:
            validation_ready = False
            validation_reason = "CANDIDATE_COMPILE_FAILED"
            log("TD candidate does not compile cleanly -> FAILED")

        if validation_ready:
            victim_oracle_audit = _td_audit_victim_oracle(
                row, module, ext_baseline, base / "Fixed",
                candidate_snapshot)
            (validation_dir / "victim_oracle_audit.json").write_text(
                json.dumps(victim_oracle_audit, indent=2), encoding="utf-8")
            if not victim_oracle_audit["ok"]:
                validation_ready = False
                validation_reason = victim_oracle_audit["reason_code"]
                log("TD victim oracle was weakened or could not be audited "
                    "soundly -> FAILED")

        if validation_ready:
            # B/P/F/FP and composed candidates stay in the external host-only
            # workspace.  Candidate-controlled Maven/test code never receives
            # a bind mount containing a reference tree.  A single disposable
            # execution copy is exposed beneath codex_outputs only while its
            # own command is running.
            oracle_work = ext / "oracle_work"
            oracle_work.mkdir(parents=True)
            td_execution_root = validation_dir / ".td_execution"
            if td_execution_root.is_symlink():
                td_execution_root.unlink()
            elif td_execution_root.exists():
                shutil.rmtree(td_execution_root)
            td_execution_root.mkdir(parents=True)

            def _cleanup_oracle_work(p=oracle_work,
                                     execution=td_execution_root):
                if execution.exists():
                    _reclaim_container_path(
                        docker_container,
                        "/app/work/" + execution.relative_to(base).as_posix())
                    shutil.rmtree(execution, ignore_errors=True)
                shutil.rmtree(p, ignore_errors=True)

            atexit.register(_cleanup_oracle_work)

            protected_sources = {
                "B": ext_baseline,
                "P": base / "FlakyCodeChange",
                "F": base / "Fixed",
                "FP": base / "FixedCodeChange",
            }
            protected_paths = {
                label: oracle_work / label for label in protected_sources
            }
            try:
                for label, source in protected_sources.items():
                    if not source.is_dir():
                        raise FileNotFoundError(
                            f"protected TD tree {label} missing: {source}")
                    shutil.copytree(
                        source, protected_paths[label], symlinks=True,
                        ignore=_evaluation_copy_ignore(source))
            except (OSError, shutil.Error) as exc:
                validation_ready = False
                validation_reason = "PROTECTED_TREE_COPY_FAILED_FAIL_CLOSED"
                oracle_build_payload = {
                    "disposition": "INCOMPLETE",
                    "code": validation_reason,
                    "message": repr(exc),
                }
                log(f"TD protected-tree copy failed: {exc!r} -> FAILED")

        oracle = None
        if validation_ready:
            build_result = build_oracle(ProtectedTrees(
                pristine=protected_paths["B"],
                perturbed=protected_paths["P"],
                fixed=protected_paths["F"],
                fixed_perturbed=protected_paths["FP"],
            ))
            oracle_build_payload = _oracle_outcome_dict(build_result.outcome)
            oracle = build_result.oracle
            if oracle is None or not build_result.outcome.passable:
                validation_ready = False
                validation_reason = (
                    build_result.outcome.code + "_FAIL_CLOSED")
                log(f"TD semantic oracle unavailable: "
                    f"{build_result.outcome.code} -> FAILED")
            else:
                oracle_manifest_payload = oracle.manifest.payload()
                (validation_dir / "oracle_manifest.json").write_text(
                    json.dumps(oracle_manifest_payload, indent=2),
                    encoding="utf-8")

        if validation_ready and oracle is not None:
            calibration_dir = validation_dir / "calibration"
            calibration_dir.mkdir(parents=True, exist_ok=True)
            calibration_counts: Counter = Counter()

            def _calibration_runner(tree: Path, _command: CommandSpec):
                label = tree.name
                calibration_counts[label] += 1
                suffix = calibration_counts[label]
                execution_tree = (
                    td_execution_root /
                    f"calibration_{label}_{suffix:02d}")
                try:
                    shutil.copytree(
                        tree, execution_tree, symlinks=True,
                        ignore=_evaluation_copy_ignore(tree))
                except (OSError, shutil.Error) as exc:
                    if execution_tree.exists() or execution_tree.is_symlink():
                        shutil.rmtree(execution_tree, ignore_errors=True)
                    return RunObservation.infra(
                        f"calibration copy failed: {exc}")
                relative = execution_tree.relative_to(base).as_posix()
                workdir = "/app/work/" + relative
                stem = f"{label}_{suffix:02d}"
                try:
                    compile_result = compile_in_container(
                        docker_container, "td", module,
                        container_workdir=workdir, clean=True,
                        log_path=calibration_dir / f"{stem}.compile.log")
                    if not compile_result["ok"]:
                        return RunObservation.infra(
                            "calibration compile failed or timed out",
                            compile_result.get("returncode"))
                    document = _verify_once(
                        container_workdir=workdir,
                        phase=f"calibration_{stem}")
                    (calibration_dir / f"{stem}.result.json").write_text(
                        json.dumps(document, indent=2), encoding="utf-8")
                    canonical_log = steps / "verify_after_fix.log"
                    if canonical_log.is_file():
                        shutil.copy2(
                            canonical_log,
                            calibration_dir / f"{stem}.verify.log")
                    return _verify_observation(document)
                finally:
                    _reclaim_container_path(docker_container, workdir)
                    shutil.rmtree(execution_tree, ignore_errors=True)

            calibration = calibrate_oracle(
                oracle,
                CommandSpec(("agentic_verify",), int(TD_SUREFIRE_TIMEOUT_S)),
                _calibration_runner,
                repetitions=1,
            )
            calibration_payload = _calibration_dict(calibration)
            (validation_dir / "calibration.json").write_text(
                json.dumps(calibration_payload, indent=2), encoding="utf-8")
            if not calibration.outcome.passable:
                validation_ready = False
                validation_reason = calibration.outcome.code + "_FAIL_CLOSED"
                log(f"TD four-tree calibration failed: "
                    f"{calibration.outcome.code} -> FAILED")

        # The official scorer applies the reference (F->FP) forcing to an
        # arbitrary candidate. The four supplied controls prove B->P fails and
        # show how F->FP behaves; they do not prove that replaying F->FP onto an
        # empty candidate B is discriminative. Validate that
        # derived no-op control explicitly or a no-op could still false-PASS.
        if validation_ready and oracle is not None and oracle_work is not None:
            reference_pristine_path = oracle_work / "B_reference_forced"
            reference_pristine_application = apply_reference_forcing(
                oracle, protected_paths["B"], reference_pristine_path,
                allow_pristine_fallback=False)
            reference_pristine_control = {
                "composition": {
                    "outcome": _oracle_outcome_dict(
                        reference_pristine_application.outcome),
                    "context": reference_pristine_application.context,
                    "files": [
                        {"path": item.path, "method": item.method}
                        for item in reference_pristine_application.files
                    ],
                },
            }
            if (not reference_pristine_application.outcome.passable
                    or reference_pristine_application.output_tree is None):
                reference_pristine_control["discriminative"] = None
            else:
                observation = _calibration_runner(
                    reference_pristine_application.output_tree,
                    CommandSpec(("agentic_verify",),
                                int(TD_SUREFIRE_TIMEOUT_S)))
                reference_pristine_control["observation"] = {
                    "outcome": observation.outcome.value,
                    "returncode": observation.returncode,
                    "tests": observation.tests,
                    "failures": observation.failures,
                    "errors": observation.errors,
                    "skipped": observation.skipped,
                    "build_success": observation.build_success,
                    "detail": observation.detail,
                }
                reference_pristine_control["discriminative"] = (
                    observation.outcome is TestRunOutcome.TEST_FAILURE)
            (validation_dir / "reference_pristine_control.json").write_text(
                json.dumps(reference_pristine_control, indent=2),
                encoding="utf-8")
            if reference_pristine_control.get("discriminative") is False:
                validation_ready = False
                validation_reason = (
                    "REFERENCE_CONTEXT_NONDISCRIMINATIVE_FAIL_CLOSED")
                log("TD reference forcing composed on pristine code but did "
                    "not produce a trustworthy test failure; it cannot "
                    "distinguish a no-op from a repair -> FAILED")

        if validation_ready and oracle is not None and oracle_work is not None:
            composition_payload = {
                "required_context": "reference",
                "contexts": {},
            }
            for forcing_context in ("reference", "pristine"):
                forced_destination = (
                    oracle_work / f"candidate_forced_{forcing_context}")
                application = apply_reference_forcing(
                    oracle, candidate_snapshot, forced_destination,
                    allow_pristine_fallback=False,
                    forcing_context=forcing_context)
                context_payload = {
                    "outcome": _oracle_outcome_dict(application.outcome),
                    "context": application.context,
                    "role": (
                        "official" if forcing_context == "reference"
                        else "diagnostic_only"),
                    "files": [
                        {"path": item.path, "method": item.method}
                        for item in application.files
                    ],
                }
                composition_payload["contexts"][forcing_context] = context_payload
                if (forcing_context == "reference"
                        and application.outcome.passable
                        and application.output_tree is not None):
                    official_contexts.append({
                        "context": forcing_context,
                        # Host-only until the context is copied into the one
                        # disposable execution slot immediately before use.
                        "source_tree": application.output_tree,
                    })
                elif forcing_context == "reference":
                    validation_ready = False
                    validation_reason = (
                        application.outcome.code + "_FAIL_CLOSED")
                    log("TD mandatory reference-context forcing could not be "
                        f"composed: {application.outcome.code} -> FAILED")
            (validation_dir / "composition.json").write_text(
                json.dumps(composition_payload, indent=2), encoding="utf-8")
    else:
        candidate_compile = compile_in_container(
            docker_container, test_type, module,
            log_path=validation_dir / "candidate_compile.log")
        if not candidate_compile["ok"]:
            validation_ready = False
            validation_reason = "CANDIDATE_COMPILE_FAILED"
        official_contexts = [{
            "context": test_type,
            "workdir": "/app/work/Flaky",
        }]

    if test_type == "id" and not id_discriminative:
        validation_ready = False
        validation_reason = "ID_NONDISCRIMINATIVE_FAIL_CLOSED"

    # Initial strict verify plus the configured number of additional passing
    # confirmations. Public outcomes are binary: any non-strict result is
    # FAILED, while its structured internal reason remains auditable.
    attempts_per_context = 1 + VERIFY_PASS_RUNS
    requested_attempts = (
        attempts_per_context * max(1, len(official_contexts))
        if test_type == "td" else attempts_per_context)
    official_documents: list[dict] = []
    stop_validation = False
    if validation_ready:
        for context_record in official_contexts:
            forcing_context = context_record["context"]
            execution_tree = None
            if test_type == "td":
                execution_tree = (
                    td_execution_root / f"official_{forcing_context}")
                try:
                    shutil.copytree(
                        Path(context_record["source_tree"]), execution_tree,
                        symlinks=True,
                        ignore=_evaluation_copy_ignore(
                            Path(context_record["source_tree"])))
                except (OSError, shutil.Error) as exc:
                    validation_ready = False
                    validation_reason = (
                        f"FORCED_CANDIDATE_{forcing_context.upper()}_"
                        "COPY_FAILED_FAIL_CLOSED")
                    log(f"TD candidate+{forcing_context} execution copy "
                        f"failed: {exc!r} -> FAILED")
                    break
                workdir = (
                    "/app/work/" + execution_tree.relative_to(base).as_posix())
            else:
                workdir = context_record["workdir"]
            try:
                if test_type == "td":
                    context_compile = compile_in_container(
                        docker_container, "td", module,
                        container_workdir=workdir, clean=True,
                        log_path=(
                            validation_dir /
                            f"forced_candidate_{forcing_context}_compile.log"))
                    forced_compile[forcing_context] = context_compile
                    if not context_compile["ok"]:
                        validation_ready = False
                        validation_reason = (
                            f"FORCED_CANDIDATE_{forcing_context.upper()}_"
                            "COMPILE_FAILED_FAIL_CLOSED")
                        log(f"TD candidate+{forcing_context} forcing does not "
                            "compile -> FAILED")
                        break
                for context_attempt in range(1, attempts_per_context + 1):
                    attempt_number = len(official_documents) + 1
                    phase = (
                        f"official_{forcing_context}_initial"
                        if context_attempt == 1 else
                        f"official_{forcing_context}_confirmation_"
                        f"{context_attempt - 1}")
                    document = _verify_once(
                        container_workdir=workdir,
                        validation_attempt=(
                            attempt_number if test_type == "td" else None),
                        phase=phase)
                    document["forcing_context"] = forcing_context
                    official_documents.append(document)
                    current = document["final_verdict"]
                    log(f"validation {attempt_number}/{requested_attempts} "
                        f"[{forcing_context}]: {current}")
                    if current != "PASSED":
                        validation_reason = str(
                            document.get("final_reason")
                            or document.get("validation_reason")
                            or "STRICT_VERIFY_FAILED")
                        stop_validation = True
                        break
            finally:
                if execution_tree is not None:
                    _reclaim_container_path(docker_container, workdir)
                    shutil.rmtree(execution_tree, ignore_errors=True)
            if stop_validation or not validation_ready:
                break

    verdict = (
        "PASSED"
        if validation_ready
        and len(official_documents) == requested_attempts
        and all(item.get("final_verdict") == "PASSED"
                for item in official_documents)
        else "FAILED"
    )
    if verdict == "PASSED":
        validation_reason = "ALL_STRICT_VALIDATIONS_PASSED"
    elif (not official_documents
          or all(item.get("final_verdict") == "PASSED"
                 for item in official_documents)):
        synthetic_status = (
            "FAILED" if validation_reason in {
                "CANDIDATE_COMPILE_FAILED",
                "VICTIM_ORACLE_WEAKENED_FAIL_CLOSED",
                "VICTIM_SOURCE_MISSING_FAIL_CLOSED",
                "VICTIM_METHOD_MISSING_FAIL_CLOSED",
            } else "INCOMPLETE")
        synthetic = {
            "schema_version": 1,
            "final_verdict": "FAILED",
            "validation_status": synthetic_status,
            "final_reason": validation_reason,
            "phase": "official_precondition",
            "attempts": [],
        }
        result_path.write_text(json.dumps(synthetic, indent=2), encoding="utf-8")
        (steps / "verify_after_fix.log").write_text(
            f"Official validation did not run: {validation_reason}\n",
            encoding="utf-8")

    confirm_runs = [
        {
            "run": index,
            "verdict": document.get("final_verdict", "FAILED"),
            "validation_status": document.get("validation_status"),
            "reason": document.get("final_reason"),
            "forcing_context": document.get("forcing_context"),
        }
        for index, document in enumerate(official_documents[1:], start=1)
    ]
    aggregate_runs = []
    valid_attempts = passed_attempts = failed_attempts = incomplete_attempts = 0
    for index, document in enumerate(official_documents, start=1):
        record = _selected_verify_record(document)
        stats = record.get("stats") or {}
        status = str(document.get("validation_status") or "")
        failures = int(stats.get("failures") or 0)
        errors = int(stats.get("errors") or 0)
        markers = int(stats.get("failure_markers") or 0)
        test_failure = (
            int(stats.get("tests") or 0) > 0
            and failures + errors + markers > 0)
        if document.get("final_verdict") == "PASSED":
            passed_attempts += 1
            valid_attempts += 1
        elif test_failure:
            failed_attempts += 1
            valid_attempts += 1
        else:
            incomplete_attempts += 1
        aggregate_runs.append({
            "attempt": index,
            "forcing_context": document.get("forcing_context"),
            "verdict": document.get("final_verdict", "FAILED"),
            "validation_status": status,
            "reason_code": (
                document.get("final_reason")
                or document.get("validation_reason")
                or record.get("reason")),
            "rc": stats.get("returncode"),
            "timed_out": stats.get("timed_out"),
            "tests": stats.get("tests", 0),
            "failures": failures,
            "errors": errors,
            "skipped": stats.get("skipped", 0),
            "build_success": stats.get("build_success", False),
            "result": (
                f"runs/attempt_{index:02d}.json"
                if test_type == "td" else "verify_after_fix.result.json"),
            "log": (
                f"runs/attempt_{index:02d}.log"
                if test_type == "td" else "verify_after_fix.log"),
        })

    model_failure_without_test = validation_reason in {
        "CANDIDATE_COMPILE_FAILED",
        "VICTIM_ORACLE_WEAKENED_FAIL_CLOSED",
        "VICTIM_SOURCE_MISSING_FAIL_CLOSED",
        "VICTIM_METHOD_MISSING_FAIL_CLOSED",
    }
    internal_status = (
        "PASSED" if verdict == "PASSED" else
        "FAILED" if failed_attempts > 0 or model_failure_without_test else
        "INCOMPLETE"
    )
    aggregate = {
        "schema_version": 1,
        "terminal_ready": True,
        "verdict": verdict,
        "internal_status": internal_status,
        "evaluation_incomplete": internal_status == "INCOMPLETE",
        "reason_code": validation_reason,
        "requested_attempts": requested_attempts,
        "actual_attempts": len(official_documents),
        "valid_attempts": valid_attempts,
        "passed_attempts": passed_attempts,
        "failed_attempts": failed_attempts,
        "incomplete_attempts": incomplete_attempts,
        "runs": aggregate_runs,
        "compile": {
            "candidate": candidate_compile,
            "forced_candidate": forced_compile,
        },
        "oracle_build": oracle_build_payload,
        "calibration": calibration_payload,
        "reference_pristine_control": reference_pristine_control,
        "composition": composition_payload,
        "victim_oracle_audit": victim_oracle_audit,
        "hashes": {
            "candidate_patch": _sha256_path(steps / "patch.diff"),
            "oracle_manifest": (
                oracle_manifest_payload.get("digest")
                if oracle_manifest_payload else None),
        },
    }
    _atomic_write_text(
        validation_dir / "aggregate.json",
        json.dumps(aggregate, indent=2) + "\n")
    if td_execution_root is not None and td_execution_root.exists():
        _reclaim_container_path(
            docker_container,
            "/app/work/" + td_execution_root.relative_to(base).as_posix())
        shutil.rmtree(td_execution_root, ignore_errors=True)
    if oracle_work is not None:
        shutil.rmtree(oracle_work, ignore_errors=True)
    log(f"verdict after {len(official_documents)} of "
        f"{requested_attempts} requested validation run(s): {verdict} "
        f"({validation_reason})")

    # ---- parse logs --------------------------------------------------------
    n_think, n_tools, usage = parse_stream(
        steps / "trial.ndjson", steps, wall_ms=agent_wall_ms)
    log(f"parsed stream: thinking_chunks={n_think} tool_calls={n_tools}")

    # ---- write Codex metadata into the canonical output folder -------------
    artifact_names = ["trial.ndjson", "codex.stderr", "patch.diff",
                      "llm_response.json", "apply_report.json",
                      "verify_after_fix.log", "verify_after_fix.verdict",
                      "verify_after_fix.result.json", "run_verdict.txt",
                      "td_validation/aggregate.json",
                      "td_validation/oracle_manifest.json",
                      "td_validation/calibration.json",
                      "td_validation/reference_pristine_control.json",
                      "td_validation/composition.json",
                      "td_validation/victim_oracle_audit.json",
                      "thinking.txt", "tool_calls.jsonl", "usage.json"]
    terminal_artifacts = {"verify_after_fix.verdict", "run_verdict.txt"}
    artifacts = {
        name: name for name in artifact_names
        if name in terminal_artifacts or (steps / name).is_file()
    }
    (steps / "meta.json").write_text(json.dumps({
        "container": container,
        "run_label": current_run_label(),
        "run_dir": str(base),
        "docker_container": docker_container,
        "model": args.model,
        "test_type": test_type,
        "module": row.get("module"),
        "polluter": row.get("polluter/state setter"),
        "victim": row.get("flaky_test"),
        "agent_exit_code": agent_rc,
        "verdict": verdict,
        "reason_code": validation_reason,
        "verify_pass_runs": VERIFY_PASS_RUNS,
        "requested_validation_attempts": requested_attempts,
        "actual_validation_attempts": len(official_documents),
        "id_discriminative": (id_discriminative if test_type == "id" else None),
        "confirm_runs": confirm_runs,
        "patch_bytes": len(diff),
        "usage": usage,
        "input_dir": "../codex_inputs",
        "artifact_dir": ".",
        "artifacts": artifacts,
    }, indent=2), encoding="utf-8")

    shutil.rmtree(ext, ignore_errors=True)
    log(f"verdict: {verdict}")
    log(f"output folder: {steps}")
    # These are the literal final fallible operations.  The binary run verdict
    # is the terminal commit marker: aggregate evidence and all metadata are
    # already durable, and reporting rejects a TD PASS without both artifacts.
    _atomic_write_text(verdict_path, verdict + "\n")
    _atomic_write_text(steps / "run_verdict.txt", verdict + "\n")
    sys.exit(0 if verdict == "PASSED" else 1)


if __name__ == "__main__":
    main()
