#!/usr/bin/env python3
"""
apply_fix.py

Apply an llm_response.json fix to the Flaky/ source tree of a result container.

Pipeline (stops at first success):
    1. git apply -p1                  (strict)
    2. git apply -p1 --recount        (tolerates wrong @@ counts)
    3. Splice output_b.fixed_code via tree-sitter-java  (AST/CST locator)
    4. Splice output_b.fixed_code via regex + masked brace scan  (fallback)

Layers 3 and 4 consume the SAME output_b.fixed_code (operation/anchor schema).
Layer 3 uses a real Java parser to locate the method / class body, so it is
immune to braces inside string-literals or comments, to call-sites that share
a method's name, and to generics / lambdas / nested classes. It is REQUIRED:
`tree-sitter` + `tree-sitter-java` must be importable (see requirements.txt;
needs a py-tree-sitter supporting Parser(Language(capsule)), >=0.22). If they
are missing the layer raises ImportError with install instructions rather than
silently falling through — silent absence would produce inconsistent apply
reports across machines and hide environmental drift in a research pipeline.
Layer 3 is transactional (snapshots all target files and rolls back every
partial write if any entry fails, so Layer 4 starts from a clean tree) and
defers genuinely ambiguous cases to Layer 4 (e.g. an `end_of_class` insert in
a file with more than one top-level type). Layer 4 — the regex splicer whose
brace scan is string/comment masked — is the fallback for those deferrals.

After a successful apply:
    - host-side `javac` smoke-tests each touched .java file (syntax check)
    - container-side `mvn test-compile` regenerates bytecode in target/
      so downstream surefire runs don't read stale .class files
The applier modifies Flaky/ in-place and writes a JSON report to
claude_outputs/apply_report.json.

Usage:
    python apply_fix.py <result_container> [--no-verify] [--no-recompile]
                                           [--docker-container NAME] [--dry-run]

The Flaky/ directory does NOT need to be a git repository — git apply
operates on plain unified diffs without an index when invoked with --check
or against a working tree.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"


def container_run_dir(container: str) -> Path:
    run_label = (os.environ.get("AGENTIC_RUN_LABEL") or "run_01").strip()
    return DATA_DIR / container / run_label


# ---------------------------------------------------------------------------
# Path resolution (shared by layer 1/2 patch applier AND layer 3 splicer)
#
# Why this exists: LLMs sometimes produce patches with paths that omit a
# Maven module prefix (e.g., `src/test/java/...` instead of
# `<module>/src/test/java/...`). Without correction, `git apply` either
# creates an orphan file at the wrong path (silent failure) or the splicer
# reports `file not found`. Either way, the verdict ends up FAILED with a
# misleading-or-empty trail.
#
# Strategy: SUFFIX match, not basename match. We only resolve a missing
# path to an existing one when the existing path *ends with* the original.
# This rules out the dangerous test/main cross-over case
# (LLM said `src/test/.../Foo.java`; ONLY existing `Foo.java` is at
# `src/main/.../Foo.java` — different suffix, no rewrite, original passes
# through and git apply fails honestly).
# ---------------------------------------------------------------------------

# Directories whose contents we never consider when resolving paths. These
# are typically build artifacts or VCS internals that may contain copies
# of source files; matching them would be actively wrong.
_PATH_FUZZY_EXCLUDED = {
    ".git", ".gradle", ".idea", ".vscode",
    "target", "build", "out", "bin", "node_modules", "dist",
}


def _resolve_path(flaky_root: Path, rel_path: str):
    """Return rel_path verbatim if it points to an existing file under
    flaky_root. Otherwise search the tree for files whose path ENDS WITH
    rel_path (suffix match), excluding build-artifact directories.
    Returns the unique match's relative path, or None if 0 / >1 matches.

    Suffix-match (not bare basename match) is critical: it prevents
    accidentally rewriting `src/test/.../Foo.java` to `src/main/.../Foo.java`
    when Foo.java exists in production code under a different sub-tree.
    """
    if not rel_path:
        return None
    norm = rel_path.replace("\\", "/").lstrip("/")
    # Agents (and the tools they read) sometimes emit paths anchored at
    # `Flaky/...` or even at the docker-side `/app/work/Flaky/...`, while
    # this function expects rel_path relative to flaky_root (i.e. with the
    # `Flaky/` component already stripped). Strip it ourselves so the agent
    # doesn't burn an iteration on a "no unique suffix match" miss. Also
    # handle the orchestrator's pristine snapshot dir for symmetry.
    for marker in ("/Flaky/", "/Flaky.pristine/"):
        if marker in norm:
            norm = norm.split(marker, 1)[1]
            break
    else:
        for prefix in ("Flaky/", "Flaky.pristine/"):
            if norm.startswith(prefix):
                norm = norm[len(prefix):]
                break
    full = flaky_root / norm
    if full.is_file():
        return norm
    basename = Path(norm).name
    if not basename:
        return None
    candidates = []
    for p in flaky_root.rglob(basename):
        if not p.is_file():
            continue
        if any(part in _PATH_FUZZY_EXCLUDED for part in p.parts):
            continue
        try:
            rel = str(p.relative_to(flaky_root)).replace("\\", "/")
        except ValueError:
            continue
        if rel == norm or rel.endswith("/" + norm):
            candidates.append(rel)
    if len(candidates) == 1:
        return candidates[0]
    return None  # 0 or >1: don't guess


def _rewrite_patch_paths(flaky_root: Path, patch_text: str):
    """Resolve every target path in the patch via _resolve_path; rewrite the
    patch text in-place where a rewrite would help. Returns
    (new_patch_text, mapping) where mapping is {original: resolved} for any
    rewrites that happened. Empty mapping means the patch is unchanged.

    Rewrites the three header forms compare-traces and git emit:
      `--- a/<path>`,  `+++ b/<path>`,  `diff --git a/<path> b/<path>`.
    /dev/null entries are left alone.
    """
    targets = _target_files_in_patch(patch_text)
    mapping = {}
    for orig in targets:
        full = flaky_root / orig.replace("\\", "/").lstrip("/")
        if full.is_file():
            continue
        resolved = _resolve_path(flaky_root, orig)
        if resolved and resolved != orig:
            mapping[orig] = resolved
    if not mapping:
        return patch_text, {}
    rewritten = patch_text
    for orig, new in mapping.items():
        oe = re.escape(orig)
        ne = new.replace("\\", "\\\\")
        # `--- a/X` and `+++ b/X`
        rewritten = re.sub(
            r'(?m)^([\-\+]{3}\s+[ab]/)' + oe + r'(\s|$)',
            lambda m: m.group(1) + ne + m.group(2),
            rewritten,
        )
        # `diff --git a/X b/X` (rare for LLM output but handle it)
        rewritten = re.sub(
            r'(?m)^(diff --git\s+a/)' + oe + r'(\s+b/)' + oe + r'(\s|$)',
            lambda m: m.group(1) + ne + m.group(2) + ne + m.group(3),
            rewritten,
        )
    return rewritten, mapping


# ---------------------------------------------------------------------------
# Layer 1 & 2: unified-diff applier (output_a)
# ---------------------------------------------------------------------------

def _ensure_trailing_newline(text: str) -> str:
    return text if text.endswith("\n") else text + "\n"


def _target_files_in_patch(patch_text: str) -> list:
    """Extract right-hand target paths from a unified diff (`+++ b/<path>`
    or plain `+++ <path>`). Skips /dev/null (file deletion). Order-preserving,
    deduplicated."""
    seen = set()
    paths = []
    for line in patch_text.splitlines():
        if not line.startswith("+++ "):
            continue
        rest = line[4:].split("\t", 1)[0].strip()
        if rest.startswith("b/"):
            rest = rest[2:]
        if not rest or rest == "/dev/null":
            continue
        if rest not in seen:
            seen.add(rest)
            paths.append(rest)
    return paths


def _fingerprint(root: Path, rel_paths: list) -> dict:
    """Return {rel_path: sha1_hex_or_None} for each file (None if missing)."""
    out = {}
    for rp in rel_paths:
        full = root / rp
        if not full.is_file():
            out[rp] = None
            continue
        h = hashlib.sha1()
        with open(full, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
        out[rp] = h.hexdigest()
    return out


def _snapshot(root: Path, rel_paths: list) -> dict:
    """Capture {rel_path: bytes_or_None} for each target file BEFORE an apply
    attempt, so we can roll back if the attempt produces an invalid result.
    None means the file did not exist."""
    snap = {}
    for rp in rel_paths:
        full = root / rp
        snap[rp] = full.read_bytes() if full.is_file() else None
    return snap


def _rollback(root: Path, snap: dict) -> None:
    """Restore each file to its snapshotted bytes. None means delete (the
    file did not exist before the apply, so delete it if it now does).
    All-or-nothing: every snapshotted file is restored, not just the ones
    that changed. This is critical for multi-file patches where some files
    landed cleanly but at least one was malformed — leaving the clean ones
    in a half-applied state would corrupt downstream layers' input."""
    for rp, content in snap.items():
        full = root / rp
        if content is None:
            if full.is_file():
                full.unlink()
        else:
            full.write_bytes(content)


def _is_valid_java_skeleton(text: str, filename: str = "") -> bool:
    """Quick structural check: does this look like a parseable Java
    compilation unit?

    Returns True for:
      - Any non-`.java` file (we don't validate config/data files).
      - `package-info.java` and `module-info.java` (special files: the
        former has no class declaration, the latter uses `module`).
      - Any other `.java` file containing BOTH a `package X.Y.Z;`
        declaration AND a top-level type declaration (class / interface /
        enum / @interface / record / module, with optional annotations
        and modifiers).

    Returns False for `.java` files missing either ingredient — typically
    because an LLM emitted only a method body without its enclosing class
    wrapper. Used by apply_patch to detect malformed file-creates and
    roll back before they pollute downstream stages.
    """
    if not filename.endswith(".java"):
        return True
    base = filename.rsplit("/", 1)[-1]
    if base in ("package-info.java", "module-info.java"):
        return True
    has_package = bool(re.search(r'^\s*package\s+[\w\.]+\s*;', text, re.M))
    # Type declaration regex tolerates:
    #   - leading whitespace
    #   - any number of @Annotation lines (optionally with arg lists)
    #   - access/abstract/final/static/sealed/non-sealed modifiers in any
    #     order (note: `non-sealed` has a hyphen; matched literally)
    #   - keyword: class | interface | enum | @interface | record | module
    has_type = bool(re.search(
        r'^\s*(?:@\w+(?:\([^)]*\))?\s+)*'
        r'(?:(?:public|protected|private|abstract|final|static|sealed|non-sealed)\s+)*'
        r'(?:class|interface|enum|@interface|record|module)\s+\w+',
        text, re.M
    ))
    return has_package and has_type


def apply_patch(flaky_root: Path, patch_text: str) -> dict:
    """Try `git apply -p1`, then `git apply -p1 --recount`. Return the first
    layer that lands or a failure record.

    A layer is considered successful only if ALL of:
      (a) git's exit code is 0,
      (b) every target file's content actually changed on disk,
      (c) every newly-CREATED file is structurally valid Java.

    Three guards against silent-failure modes seen in practice:

    1. Path fuzzy-match (idea 1) — before any git apply, we resolve each
       target path against the actual file tree using suffix matching.
       This catches LLM patches that omit a Maven module prefix (e.g.,
       `src/test/java/...` instead of `<module>/src/test/java/...`).

    2. Hash-based silent-skip detection (idea 3a) — `git apply` outside a
       git repo will print 'Skipped patch <path>.' and return exit 0 when
       it can't find the context cleanly (often due to bad line numbers).
       We compare before/after hashes to catch this.

    3. Malformed-create check (idea 3b) — when the LLM emits a method body
       without its enclosing class wrapper plus a `@@ -0,0 +1,N @@` (file-
       create) header, `git apply` happily creates an orphan file with no
       package/class declaration. We validate any newly-created `.java`
       file has `package X.Y.Z;` AND a top-level type declaration; if not,
       we roll back and fall through to the next layer.

    Failure modes that survive these checks should genuinely be patch
    rejections (not silent-success-then-mysterious-FAILED-verdict).
    """
    if not patch_text or not patch_text.strip():
        return {"layer": None, "ok": False, "reason": "empty patch"}

    patch_text = _ensure_trailing_newline(patch_text)

    # IDEA 1: rewrite paths in the patch text BEFORE git apply sees it.
    # Mapping is empty if every target path already exists.
    patch_text, path_map = _rewrite_patch_paths(flaky_root, patch_text)
    targets = _target_files_in_patch(patch_text)

    layers = [
        ("git apply",            ["git", "apply", "-p1"]),
        ("git apply --recount",  ["git", "apply", "-p1", "--recount"]),
    ]

    last_err = None
    for name, cmd in layers:
        check = subprocess.run(
            cmd + ["--check"],
            input=patch_text, text=True, cwd=flaky_root,
            capture_output=True,
        )
        if check.returncode != 0:
            last_err = check.stderr.strip()
            continue

        # Snapshot every target's pre-apply state. Used both for the silent-
        # skip hash comparison and for all-or-nothing rollback if a later
        # validation step rejects the apply.
        snap = _snapshot(flaky_root, targets)
        before = {p: hashlib.sha1(b).hexdigest() if b is not None else None
                  for p, b in snap.items()}
        apply = subprocess.run(
            cmd,
            input=patch_text, text=True, cwd=flaky_root,
            capture_output=True,
        )
        if apply.returncode != 0:
            last_err = apply.stderr.strip()
            continue

        # Hash check (silent-skip detection — idea 3a, established earlier).
        after = _fingerprint(flaky_root, targets)
        unchanged = [p for p in targets if before.get(p) == after.get(p)]
        if targets and unchanged:
            log_tail = (apply.stdout + apply.stderr).strip().splitlines()[-3:]
            last_err = (
                f"{name} returned 0 but {len(unchanged)}/{len(targets)} "
                f"target files unchanged (silent skip): {unchanged}. "
                f"git tail: {log_tail!r}"
            )
            # Nothing changed → nothing to roll back.
            continue

        # Malformed-create check (idea 3b). Newly-created files must look
        # like real Java compilation units; otherwise the LLM probably gave
        # us a bare method body without a class wrapper.
        created = [p for p in targets
                   if before.get(p) is None and after.get(p) is not None]
        malformed = []
        for p in created:
            try:
                text = (flaky_root / p).read_text(encoding="utf-8", errors="replace")
            except OSError:
                text = ""
            if not _is_valid_java_skeleton(text, filename=p):
                malformed.append(p)
        if malformed:
            _rollback(flaky_root, snap)
            last_err = (
                f"{name} returned 0 but created malformed Java files (no "
                f"`package` declaration and/or no top-level class): "
                f"{malformed}. The LLM likely emitted a method body without "
                f"its enclosing class wrapper. Rolled back."
            )
            continue

        result = {"layer": name, "ok": True}
        if path_map:
            result["path_rewritten"] = path_map
        return result

    # IDEA 4: 3-way merge fallback. The strict and --recount applies above are
    # `--check`-gated and so reject any context/whitespace drift. `git apply
    # --3way` can still land such a patch by merging against the patch's base
    # blobs — but ONLY when flaky_root is a git repo whose object store holds
    # those blobs (the agentic_claude_cli driver commits a baseline before
    # scoring). --3way cannot be --check-gated (the gate would fail the same
    # way), so we snapshot first and roll back on any non-clean outcome. On the
    # orchestrator's non-repo trees this just errors out and rolls back — a
    # harmless no-op that never changes prior behavior.
    snap = _snapshot(flaky_root, targets)
    before = {p: hashlib.sha1(b).hexdigest() if b is not None else None
              for p, b in snap.items()}
    apply3 = subprocess.run(
        ["git", "apply", "-p1", "--3way"],
        input=patch_text, text=True, cwd=flaky_root, capture_output=True,
    )
    if apply3.returncode == 0:
        after = _fingerprint(flaky_root, targets)
        unchanged = [p for p in targets if before.get(p) == after.get(p)]
        conflicted = []
        created = [p for p in targets
                   if before.get(p) is None and after.get(p) is not None]
        malformed = []
        for p in targets:
            fp = flaky_root / p
            if not fp.is_file():
                continue
            try:
                t = fp.read_text(encoding="utf-8", errors="replace")
            except OSError:
                t = ""
            if "<<<<<<<" in t and ">>>>>>>" in t:
                conflicted.append(p)
            if p in created and not _is_valid_java_skeleton(t, filename=p):
                malformed.append(p)
        if targets and unchanged:
            last_err = f"git apply --3way returned 0 but unchanged: {unchanged}"
        elif conflicted:
            _rollback(flaky_root, snap)
            last_err = f"git apply --3way left conflict markers: {conflicted}. Rolled back."
        elif malformed:
            _rollback(flaky_root, snap)
            last_err = f"git apply --3way created malformed Java: {malformed}. Rolled back."
        else:
            result = {"layer": "git apply --3way", "ok": True}
            if path_map:
                result["path_rewritten"] = path_map
            return result
    else:
        _rollback(flaky_root, snap)
        last_err = (apply3.stderr or apply3.stdout).strip() or "git apply --3way failed"

    out = {"layer": None, "ok": False,
           "reason": last_err or "all patch layers rejected"}
    if path_map:
        out["path_rewritten"] = path_map
    return out


def check_patch(flaky_root: Path, patch_text: str) -> dict:
    """Dry-run variant: report which layer would land, without modifying files.
    Mirrors apply_patch's path fuzzy-match so dry-run results agree with
    real-run results — but does NOT replicate the post-apply hash and
    malformed-create checks (which require an actual apply to evaluate)."""
    if not patch_text:
        return {"layer": None, "ok": False, "reason": "empty patch"}
    patch_text = _ensure_trailing_newline(patch_text)
    patch_text, path_map = _rewrite_patch_paths(flaky_root, patch_text)
    for name, cmd in [
        ("git apply",           ["git", "apply", "-p1", "--check"]),
        ("git apply --recount", ["git", "apply", "-p1", "--recount", "--check"]),
    ]:
        r = subprocess.run(cmd, input=patch_text, text=True,
                           cwd=flaky_root, capture_output=True)
        if r.returncode == 0:
            result = {"layer": name, "ok": True}
            if path_map:
                result["path_rewritten"] = path_map
            return result
    out = {"layer": None, "ok": False, "reason": "all patch layers rejected"}
    if path_map:
        out["path_rewritten"] = path_map
    return out


# ---------------------------------------------------------------------------
# Layer 3: structured splicer (output_b.fixed_code)
# ---------------------------------------------------------------------------

# ---- Auto-import inference (idea 2) ---------------------------------------
# Popular (NOT exhaustive) annotation -> import path lookup. When the splicer
# inserts code that references @SomeAnnotation that the file doesn't already
# import, we look it up here and add the right import. Unknown annotations
# are silently skipped (we never guess at unfamiliar names — better to let
# compile fail honestly than to invent a wrong import).
#
# Format: simple_name -> (junit4_path, junit5_path_or_None).
# When the file already imports anything from `org.junit.jupiter.*`, we
# treat it as a JUnit 5 file and prefer the second path (if present);
# otherwise we use the first.
_KNOWN_ANNOTATIONS = {
    # JUnit 4 / 5 lifecycle (JUnit 5 uses different simple names for
    # @BeforeEach/@AfterEach/@BeforeAll/@AfterAll — those are listed
    # separately below as their own JUnit-5-only entries).
    "Test":         ("org.junit.Test",            "org.junit.jupiter.api.Test"),
    "Before":       ("org.junit.Before",          "org.junit.jupiter.api.BeforeEach"),
    "After":        ("org.junit.After",           "org.junit.jupiter.api.AfterEach"),
    "BeforeClass":  ("org.junit.BeforeClass",     "org.junit.jupiter.api.BeforeAll"),
    "AfterClass":   ("org.junit.AfterClass",      "org.junit.jupiter.api.AfterAll"),
    "Ignore":       ("org.junit.Ignore",          "org.junit.jupiter.api.Disabled"),
    "RunWith":      ("org.junit.runner.RunWith",  None),  # JUnit 4 only
    "Rule":         ("org.junit.Rule",            None),
    "ClassRule":    ("org.junit.ClassRule",       None),
    "Parameters":   ("org.junit.runners.Parameterized.Parameters", None),

    # JUnit 5 (Jupiter) — annotations that have no JUnit 4 counterpart.
    # First slot doubles as the JUnit-5 path so the lookup works regardless
    # of detected framework (these names ONLY exist in JUnit 5).
    "BeforeEach":   ("org.junit.jupiter.api.BeforeEach",  None),
    "AfterEach":    ("org.junit.jupiter.api.AfterEach",   None),
    "BeforeAll":    ("org.junit.jupiter.api.BeforeAll",   None),
    "AfterAll":     ("org.junit.jupiter.api.AfterAll",    None),
    "Disabled":     ("org.junit.jupiter.api.Disabled",    None),
    "DisplayName":  ("org.junit.jupiter.api.DisplayName", None),
    "Nested":       ("org.junit.jupiter.api.Nested",      None),
    "Tag":          ("org.junit.jupiter.api.Tag",         None),
    "ExtendWith":   ("org.junit.jupiter.api.extension.ExtendWith", None),
    "ParameterizedTest": ("org.junit.jupiter.params.ParameterizedTest", None),
    "ValueSource":  ("org.junit.jupiter.params.provider.ValueSource", None),
    "MethodSource": ("org.junit.jupiter.params.provider.MethodSource", None),
    "RepeatedTest": ("org.junit.jupiter.api.RepeatedTest", None),
    "Timeout":      ("org.junit.jupiter.api.Timeout",     None),
    "TempDir":      ("org.junit.jupiter.api.io.TempDir",  None),

    # Mockito (no framework split needed).
    "Mock":         ("org.mockito.Mock",          None),
    "Spy":          ("org.mockito.Spy",           None),
    "Captor":       ("org.mockito.Captor",        None),
    "InjectMocks":  ("org.mockito.InjectMocks",   None),
}


def _existing_import_simple_names(src: str) -> set:
    """{'Before', 'Test', 'JobRegistry', ...} — last-segment of every
    `import x.y.Z;` (incl. `import static`) currently in the file."""
    names = set()
    for m in re.finditer(
        r'^\s*import\s+(?:static\s+)?[\w\.]+\.(\w+)\s*;', src, re.M
    ):
        names.add(m.group(1))
    return names


def _detect_test_framework(src: str) -> str:
    """Returns 'junit5' if the file imports anything from
    org.junit.jupiter.*, else 'junit4'. Used to disambiguate annotations
    whose simple name is shared between JUnit 4 and JUnit 5 (e.g. @Test)."""
    if re.search(r'^\s*import\s+org\.junit\.jupiter\.', src, re.M):
        return "junit5"
    return "junit4"


def _infer_imports_for_inserted_code(src: str, inserted_code: str) -> list:
    """Scan `inserted_code` for @Annotation references that are not already
    importable from `src`. For each unknown annotation that appears in
    _KNOWN_ANNOTATIONS, return the corresponding `import X;` line.

    Unknown annotations are silently skipped — we never invent imports
    for names we don't recognise. Fully-qualified annotations (`@org.x.Y`)
    capture only `org` (which is not in the table) and are also skipped,
    correctly avoiding double-imports of names the LLM already qualified."""
    if not inserted_code:
        return []
    have = _existing_import_simple_names(src)
    framework = _detect_test_framework(src)
    out, seen = [], set()
    for m in re.finditer(r'@(\w+)', inserted_code):
        name = m.group(1)
        if name in have or name in seen:
            continue
        if name not in _KNOWN_ANNOTATIONS:
            continue
        junit4, junit5 = _KNOWN_ANNOTATIONS[name]
        # When there's a JUnit-5 alternative AND the file looks like a
        # JUnit-5 file, prefer it. Otherwise use the first slot (which is
        # always the canonical/default path for the annotation).
        path = junit5 if (framework == "junit5" and junit5) else junit4
        if path:
            out.append(f"import {path};")
            seen.add(name)
    return out
# ---- end auto-import inference --------------------------------------------

# Java method signature pattern: leading whitespace, optional INLINE
# annotations (e.g. `@Test`, `@Test(timeout=1000)`), optional modifiers,
# return type, name, parameter list, optional throws, opening brace.
# Annotation prefix is required because junit-quickcheck (and many other
# real codebases) write methods on a single line as
#     @Test public void foo() throws Exception {
# and without the annotation prefix the regex won't anchor at column 0
# (it would find `public void foo` mid-line, but `re.search` over the
# whole line is what we want — see the multiline ^ anchor).
# Modifiers and return type intentionally permissive — we don't validate
# Java; we just want to find the line where `<name>(...)` is declared.
_METHOD_PATTERN_TMPL = (
    r'^([ \t]*)'                                            # leading indent
    r'(?:@\w+(?:\([^)]*\))?\s+)*'                           # inline annotations: @Test, @Test(timeout=N), @Override, ...
    r'(?:(?:public|private|protected|static|final|'
    r'synchronized|abstract|default|native)\s+)*'           # modifiers
    r'[\w<>\[\]\?,\s\.]+\s+'                                # return type
    r'{name}\s*\([^)]*\)\s*'                                # name(args)
    r'(?:throws\s[^{{]+)?\{{'                               # optional throws + brace
)


def parse_imports_field(imports_field) -> list:
    """Normalise the @@IMPORTS field to a list of `import X;` strings."""
    if not imports_field:
        return []
    if isinstance(imports_field, list):
        lines = imports_field
    else:
        lines = imports_field.split("\n")
    out = []
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        if not ln.startswith("import "):
            ln = "import " + ln
        if not ln.endswith(";"):
            ln += ";"
        out.append(ln)
    return out


def add_imports(src: str, new_imports: list) -> str:
    """Add imports that aren't already present, after the last existing
    import statement. Idempotent: re-running adds nothing."""
    if not new_imports:
        return src

    have = set()
    for m in re.finditer(r'^\s*(import\s+(?:static\s+)?[\w\.\*]+\s*;)', src, re.M):
        have.add(re.sub(r'\s+', ' ', m.group(1)).strip())

    to_add = []
    for imp in new_imports:
        norm = re.sub(r'\s+', ' ', imp).strip()
        if norm not in have:
            to_add.append(imp)
            have.add(norm)
    if not to_add:
        return src

    # Use `[ \t]*` (NOT `\s*`) at both ends so we only match the import on
    # its own line. Greedy `\s*$` would consume trailing newlines AND any
    # following blank lines, parking insert_at right before the next non-blank
    # line (typically the class declaration) — which would emit the new
    # import disconnected from its sibling import block. `[ \t]*` keeps the
    # match within a single line; insert_at lands cleanly after the `;`.
    last_import = None
    for m in re.finditer(r'^[ \t]*import\s+[^;]+;[ \t]*$', src, re.M):
        last_import = m
    if last_import:
        insert_at = last_import.end()
        return src[:insert_at] + "\n" + "\n".join(to_add) + src[insert_at:]

    pkg = re.search(r'^\s*package\s+[^;]+;\s*$', src, re.M)
    if pkg:
        return src[:pkg.end()] + "\n\n" + "\n".join(to_add) + src[pkg.end():]
    return "\n".join(to_add) + "\n\n" + src


def _normalise_param_list(params: str) -> str:
    """'final @NotNull int x, String y' -> 'int,String'. Strips parameter
    names, annotations, the `final` modifier, and whitespace, leaving only
    the types in declaration order. Used to compare LLM-provided method
    signatures against on-file overloads."""
    if not params or not params.strip():
        return ""
    out = []
    for p in params.split(","):
        p = p.strip()
        if not p:
            continue
        # Strip leading annotations like `@NotNull` or `@Param("x")`.
        p = re.sub(r'@\w+(?:\([^)]*\))?\s*', '', p).strip()
        # Strip 'final' modifier.
        p = re.sub(r'\bfinal\b\s*', '', p).strip()
        # The PARAMETER NAME is the last whitespace-separated token; the
        # TYPE is everything before it. Generics like `List<String>` are
        # preserved verbatim (no whitespace inside `<...>` after collapse).
        tokens = p.split()
        type_part = " ".join(tokens[:-1]) if len(tokens) >= 2 else tokens[0]
        out.append(re.sub(r'\s+', '', type_part))
    return ",".join(out)


def _annotations_in_method_block(code: str) -> list:
    """Extract the set of @Annotation names from the method block's prologue
    (everything before the first `{`). Order-preserving, deduplicated."""
    brace = code.find("{")
    head = code if brace == -1 else code[:brace]
    return list(dict.fromkeys(m.group(1) for m in re.finditer(r'@(\w+)', head)))


def _params_in_method_block(code: str, name: str) -> str:
    """Extract the parameter list for the named method's signature in the
    LLM's code block, normalised to types-only."""
    m = re.search(rf'\b{re.escape(name)}\s*\(([^)]*)\)', code)
    return _normalise_param_list(m.group(1)) if m else None


def _annotations_at_match(src: str, head_offset: int) -> list:
    """Find @Annotation names on the signature line at head_offset PLUS any
    annotations on contiguous preceding lines. Mirrors the file's full
    annotation set for the method whose signature begins at head_offset."""
    sig_end = src.find("\n", head_offset)
    if sig_end == -1:
        sig_end = len(src)
    sig_line_start = src.rfind("\n", 0, head_offset) + 1
    sig_line = src[sig_line_start:sig_end]

    prior = []
    pre_lines = src[:sig_line_start].splitlines()
    for ln in reversed(pre_lines):
        s = ln.strip()
        if not s:
            continue
        if s.startswith("@"):
            am = re.match(r'@(\w+)', s)
            if am:
                prior.append(am.group(1))
        else:
            break

    inline = [m.group(1) for m in re.finditer(r'@(\w+)', sig_line)]
    return list(dict.fromkeys(list(reversed(prior)) + inline))


def _code_mask(src: str) -> str:
    r"""Return a copy of `src` with the *contents* of string literals, char
    literals, and comments replaced by spaces (newlines preserved), leaving
    all real code — including braces — intact. Length and byte offsets are
    preserved, so an offset found in the mask is valid against `src`.

    This lets brace/structure scanners ignore braces inside strings or
    comments — e.g. a malformed-JSON fixture like "{\"k\":\"v\"" whose '{'
    has no matching '}' and would otherwise unbalance a naive counter.

    Not handled: Java text blocks (\"\"\" ... \"\"\", Java 15+). The JDK 8/11
    corpus in this benchmark does not use them.
    """
    out = []
    i, n = 0, len(src)
    NORMAL, LINE, BLOCK, STR, CHAR = range(5)
    state = NORMAL
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == NORMAL:
            if c == "/" and nxt == "/":
                state = LINE; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                state = BLOCK; out.append("  "); i += 2; continue
            if c == '"':
                state = STR; out.append(" "); i += 1; continue
            if c == "'":
                state = CHAR; out.append(" "); i += 1; continue
            out.append(c); i += 1; continue
        if state == LINE:
            out.append("\n" if c == "\n" else " ")
            if c == "\n":
                state = NORMAL
            i += 1; continue
        if state == BLOCK:
            if c == "*" and nxt == "/":
                state = NORMAL; out.append("  "); i += 2; continue
            out.append("\n" if c == "\n" else " "); i += 1; continue
        # STR or CHAR: handle escapes so an escaped quote does not close it.
        if c == "\\":
            if nxt:
                out.append("  "); i += 2
            else:
                out.append(" "); i += 1
            continue
        if (state == STR and c == '"') or (state == CHAR and c == "'"):
            state = NORMAL; out.append(" "); i += 1; continue
        out.append("\n" if c == "\n" else " "); i += 1; continue
    return "".join(out)


def _expand_method_loc(src: str, m):
    """Given a regex match for a method-signature line, walk backwards over
    leading @Annotation lines and forward via brace-balance to find the
    method's full extent. Returns (head, end) byte offsets, or None if the
    body braces don't balance. Brace-balance runs over a string/comment mask
    so braces inside literals or comments don't throw off the count."""
    body_brace = m.end() - 1
    head = m.start()

    pre_lines = src[:head].splitlines(keepends=True)
    while pre_lines:
        last = pre_lines[-1]
        stripped = last.lstrip()
        if stripped.startswith("@"):
            head -= len(last)
            pre_lines.pop()
        elif last.strip() == "" and len(pre_lines) >= 2 \
                and pre_lines[-2].lstrip().startswith("@"):
            head -= len(last)
            pre_lines.pop()
        else:
            break

    mask = _code_mask(src)
    depth = 0
    i = body_brace
    while i < len(src):
        c = mask[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                if end < len(src) and src[end] == "\n":
                    end += 1
                return (head, end)
        i += 1
    return None


def find_method(src: str, name: str, llm_code: str = None):
    """Locate a method by name. Returns (head, end) byte offsets including
    leading annotation lines, or None if no match.

    When `llm_code` is provided (the @@METHOD code block from output_b),
    disambiguates among multiple file occurrences with the same name by
    scoring each candidate against the LLM's intended annotations and
    parameter list. This is critical for files with shared names between
    outer test methods and inner helper classes — for example a JUnit-style
    test class whose inner @Property class declares a method with the same
    name as the outer @Test method. Without disambiguation, the splicer
    would silently rewrite whichever appeared first by line number, which
    is usually NOT the intended target.

    Scoring (per candidate):
      +1 per @Annotation name shared with the LLM's intent
      +5 if the parameter type list matches exactly
    Highest score wins; ties resolved by file order. Falls back to the
    first match when llm_code is missing or no candidate scores above 0.
    """
    pat = re.compile(_METHOD_PATTERN_TMPL.format(name=re.escape(name)), re.M)
    # Match signatures against the string/comment mask so a method-like line
    # inside a comment or string can't be picked up. Offsets stay valid in src.
    matches = list(pat.finditer(_code_mask(src)))
    if not matches:
        return None

    if len(matches) == 1 or not llm_code:
        return _expand_method_loc(src, matches[0])

    intent_annos = _annotations_in_method_block(llm_code)
    intent_params = _params_in_method_block(llm_code, name)

    best = matches[0]
    best_score = -1
    for m in matches:
        cand_annos = _annotations_at_match(src, m.start())
        cand_params = _normalise_param_list(
            re.search(rf'\b{re.escape(name)}\s*\(([^)]*)\)',
                      src[m.start():m.end()]).group(1)
        )
        score = 0
        for a in intent_annos:
            if a in cand_annos:
                score += 1
        if intent_params is not None and intent_params == cand_params:
            score += 5
        if score > best_score:
            best_score = score
            best = m
    return _expand_method_loc(src, best)


def find_outer_class_close(src: str):
    """Return the offset of the outermost class's closing '}', or None.
    Scans a string/comment mask so braces inside literals/comments are
    ignored (offsets remain valid against the original src)."""
    mask = _code_mask(src)
    depth = 0
    in_class = False
    last = -1
    for i, c in enumerate(mask):
        if c == "{":
            in_class = True
            depth += 1
        elif c == "}" and in_class:
            depth -= 1
            if depth == 0:
                last = i
    return last if last >= 0 else None


def get_indent_at(src: str, offset: int) -> str:
    """Return the leading whitespace of the line containing `offset`."""
    line_start = src.rfind("\n", 0, offset) + 1
    line_end = src.find("\n", line_start)
    if line_end == -1:
        line_end = len(src)
    line = src[line_start:line_end]
    return re.match(r'^[ \t]*', line).group(0)


def reindent_block(code: str, target_indent: str) -> str:
    """Reindent every line of `code` so the first non-empty line gets exactly
    `target_indent`, with all other lines preserving their relative indentation
    to that first line. Empty/whitespace-only lines pass through unchanged.

    Handles both LLM authoring styles uniformly:
      - Code at column 0 (most common — the LLM writes the method as a
        standalone block): every line gets `target_indent` prepended.
      - Code already pre-indented to some absolute level (e.g. the LLM
        chose the class's indent level itself): that base indent is
        substituted for `target_indent`, and relative indents to deeper
        body lines are preserved.

    Replaces the older reindent_first_line, which only adjusted line 1 and
    left body lines unchanged — that produced inconsistent formatting (and
    mis-indented closing braces) when the LLM wrote the method starting at
    column 0, as Claude Sonnet 4.6 does in practice."""
    lines = code.split("\n")
    first_idx = next((i for i, ln in enumerate(lines) if ln.strip()), None)
    if first_idx is None:
        return code
    base = re.match(r'^[ \t]*', lines[first_idx]).group(0)
    out = []
    for ln in lines:
        if not ln.strip():
            out.append(ln)
        elif ln.startswith(base):
            out.append(target_indent + ln[len(base):])
        else:
            # Line is less indented than the first non-empty line — unusual
            # (the LLM wrote misaligned code). Pass through unchanged rather
            # than crash; downstream javac will catch real syntax issues.
            out.append(ln)
    return "\n".join(out)


# Backwards-compat alias: any external caller (none currently exist) still
# works. Internal sites have been migrated to reindent_block.
reindent_first_line = reindent_block


def parse_anchor(anchor: str):
    """Parse @@ANCHOR value. Returns (kind, target_or_none) or None."""
    if not anchor:
        return None
    s = anchor.strip()
    m = re.match(r'(before_method|after_method)\s*=\s*(\w+)\s*$', s)
    if m:
        return (m.group(1), m.group(2))
    if s == "end_of_class":
        return ("end_of_class", None)
    return None


def apply_fixed_code_entry(src: str, entry: dict) -> tuple:
    """Apply ONE fixed_code entry to file content. Returns (new_src, info_dict).
    Raises ValueError on schema violations or unfindable anchors.

    Phases (in order):
      1. Add the entry's explicitly-listed @@IMPORTS to `src`.
      2. Splice the @@METHOD code per @@OPERATION (replace_method or
         insert_method with @@ANCHOR). Collected into `result_src`
         (single return point so the post-step can run uniformly).
      3. Auto-import inference: scan the inserted code for @Annotation
         references that the file doesn't already import; add any that
         match the well-known table (idea 2).
    """
    info = {
        "file": entry.get("file"),
        "method": entry.get("method"),
        "operation": entry.get("operation") or "replace_method",
    }

    # Phase 1: explicit imports from the entry.
    imports = parse_imports_field(entry.get("imports"))
    src = add_imports(src, imports)
    info["imports_added"] = len(imports)

    # Phase 2: method splice. Collect into result_src for a single return.
    operation = info["operation"]
    method_name = entry.get("method")
    code = entry.get("code") or ""

    if not method_name or not code:
        raise ValueError(f"missing method or code field: {entry}")

    result_src = None
    if operation == "replace_method":
        # Pass the LLM's code so find_method can disambiguate when the file
        # has multiple methods with this name (inner-vs-outer, overloads).
        loc = find_method(src, method_name, llm_code=code)
        if loc is None:
            raise ValueError(
                f"replace_method: method {method_name!r} not found in {info['file']}")
        indent = get_indent_at(src, loc[0])
        new_code = reindent_block(code, indent)
        if not new_code.endswith("\n"):
            new_code += "\n"
        result_src = src[:loc[0]] + new_code + src[loc[1]:]

    elif operation == "insert_method":
        if find_method(src, method_name) is not None:
            raise ValueError(
                f"insert_method: a method named {method_name!r} already exists "
                f"in {info['file']} — operation contradicts file state")

        anchor = parse_anchor(entry.get("anchor"))
        if anchor is None:
            info["anchor_warning"] = "missing or invalid anchor; defaulting to end_of_class"
            anchor = ("end_of_class", None)
        kind, target = anchor

        if kind in ("before_method", "after_method"):
            loc = find_method(src, target)
            if loc is None:
                raise ValueError(
                    f"insert_method: anchor target {target!r} not found in {info['file']}")
            indent = get_indent_at(src, loc[0])
            new_code = reindent_block(code, indent)
            if kind == "before_method":
                result_src = src[:loc[0]] + new_code + "\n\n" + src[loc[0]:]
            else:
                # loc[1] already includes a trailing newline
                result_src = src[:loc[1]] + "\n" + new_code + "\n" + src[loc[1]:]

        elif kind == "end_of_class":
            close = find_outer_class_close(src)
            if close is None:
                raise ValueError(f"end_of_class: outer class brace not found in {info['file']}")
            new_code = reindent_block(code, "    ")
            result_src = src[:close] + "\n" + new_code + "\n" + src[close:]

    if result_src is None:
        raise ValueError(f"unknown operation: {operation!r}")

    # Phase 3: auto-import inference for annotations referenced by the
    # inserted code. Idempotent w.r.t. phase 1 — anything already imported
    # (whether by the file originally or just-added in phase 1) is skipped.
    inferred = _infer_imports_for_inserted_code(result_src, code)
    if inferred:
        result_src = add_imports(result_src, inferred)
        info["imports_inferred"] = inferred

    return result_src, info


def apply_fixed_code(flaky_root: Path, entries: list) -> dict:
    """Apply every fixed_code entry. Multiple entries on the same file are
    applied sequentially against the evolving file content (the second
    entry sees the first entry's edits).

    Per-entry path resolution (idea 1): if `entry['file']` doesn't exist
    under flaky_root, _resolve_path searches the tree for a file whose
    path ends with the requested suffix. Unique match -> use the resolved
    path; 0 or >1 matches -> fail this entry honestly with `file not found`.
    """
    if not entries:
        return {"layer": None, "ok": False, "reason": "no fixed_code entries"}

    applied = []
    failed = []

    for entry in entries:
        rel = entry.get("file")
        if not rel:
            failed.append({"entry": entry, "reason": "missing file field"})
            continue
        # IDEA 1: try to resolve a wrong/short path to a real one.
        resolved = _resolve_path(flaky_root, rel)
        if resolved is None:
            failed.append({
                "entry": {k: v for k, v in entry.items() if k != "code"},
                "reason": f"file not found: {rel} (no unique suffix match in flaky tree)",
            })
            continue
        if resolved != rel:
            entry = dict(entry, file=resolved)  # don't mutate caller's dict
        target = flaky_root / resolved
        try:
            src = target.read_text(encoding="utf-8")
            new_src, info = apply_fixed_code_entry(src, entry)
            target.write_text(new_src, encoding="utf-8")
            info["abs_path"] = str(target)
            if resolved != rel:
                info["path_resolved"] = {"original": rel, "resolved": resolved}
            applied.append(info)
        except Exception as e:
            failed.append({
                "entry": {k: v for k, v in entry.items() if k != "code"},
                "reason": str(e),
            })

    return {
        "layer": "splice output_b",
        "ok": len(failed) == 0 and len(applied) > 0,
        "applied": applied,
        "failed": failed,
    }


# ---------------------------------------------------------------------------
# Layer (tree-sitter): structured splice via a real Java parser
#
# Same job as apply_fixed_code (the output_b.fixed_code splicer), but locates
# methods and the class-body close with tree-sitter-java instead of regex +
# brace counting. Immune to braces in strings/comments, call-sites that share
# a method's name, generics, lambdas, nested/inner classes and enums. Optional:
# if tree-sitter isn't installed it's skipped and the regex splicer remains the
# fallback. Reuses the regex splicer's import handling for parity.
# ---------------------------------------------------------------------------

_TS_STATE = {"tried": False, "parser": None, "error": None}
_TS_TYPE_DECLS = ("class_declaration", "interface_declaration",
                  "enum_declaration", "record_declaration",
                  "annotation_type_declaration")
_TS_METHOD_DECLS = ("method_declaration", "constructor_declaration")


def _ts_parser():
    """Lazily build a cached tree-sitter-java parser. REQUIRED — raises a
    clear ImportError with install instructions if tree-sitter or
    tree-sitter-java is missing or too old. Failing loud is intentional: a
    silent fallback to the regex splicer would hide environmental drift and
    make reports inconsistent across machines."""
    if not _TS_STATE["tried"]:
        _TS_STATE["tried"] = True
        try:
            import tree_sitter_java
            from tree_sitter import Language, Parser
            _TS_STATE["parser"] = Parser(Language(tree_sitter_java.language()))
        except Exception as exc:
            _TS_STATE["error"] = exc
    if _TS_STATE["parser"] is None:
        raise ImportError(
            "apply_fix.py requires the tree-sitter Java parser for its "
            "structured (output_b.fixed_code) splice layer.\n"
            "Install with:\n"
            "    python3 -m pip install -r requirements.txt\n"
            "or directly:\n"
            "    python3 -m pip install tree-sitter>=0.22 tree-sitter-java>=0.23\n"
            "(on Homebrew / PEP-668 Python add --break-system-packages)\n"
            f"Underlying import error: {_TS_STATE['error']!r}"
        )
    return _TS_STATE["parser"]


def _ts_walk(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _ts_method_name(node):
    nm = node.child_by_field_name("name")
    return nm.text.decode("utf-8", "replace") if nm is not None else None


def _ts_method_annotations(node):
    """Annotation simple-names on a method node (from its modifiers child)."""
    out = []
    for c in node.children:
        if c.type == "modifiers":
            for d in _ts_walk(c):
                if d.type in ("marker_annotation", "annotation"):
                    nm = d.child_by_field_name("name")
                    if nm is not None:
                        out.append(nm.text.decode("utf-8", "replace").split(".")[-1])
            break
    return out


def _ts_method_param_norm(node):
    fp = node.child_by_field_name("parameters")
    if fp is None:
        return ""
    inner = fp.text.decode("utf-8", "replace").strip()
    if inner.startswith("(") and inner.endswith(")"):
        inner = inner[1:-1]
    return _normalise_param_list(inner)


def _ts_indent_of(src_bytes, offset):
    line_start = src_bytes.rfind(b"\n", 0, offset) + 1
    i = line_start
    while i < offset and src_bytes[i:i + 1] in (b" ", b"\t"):
        i += 1
    return src_bytes[line_start:i].decode("utf-8", "replace")


def _ts_find_method(tree, name, llm_code=None):
    """Method/constructor node named `name`, disambiguating among multiple by
    annotation overlap + param match (mirrors find_method's scoring)."""
    cands = [n for n in _ts_walk(tree.root_node)
             if n.type in _TS_METHOD_DECLS and _ts_method_name(n) == name]
    if not cands:
        return None
    if len(cands) == 1 or not llm_code:
        return cands[0]
    intent_annos = _annotations_in_method_block(llm_code)
    intent_params = _params_in_method_block(llm_code, name)
    best, best_score = cands[0], -1
    for n in cands:
        score = sum(1 for a in intent_annos if a in _ts_method_annotations(n))
        if intent_params is not None and intent_params == _ts_method_param_norm(n):
            score += 5
        if score > best_score:
            best_score, best = score, n
    return best


def _ts_outer_class_close(tree):
    """Byte offset of the sole top-level type's class-body closing brace.
    Returns None for 0 or >1 top-level types — end_of_class is ambiguous in
    a multi-type file, so we defer to the regex layer rather than guess."""
    types = [c for c in tree.root_node.children if c.type in _TS_TYPE_DECLS]
    if len(types) != 1:
        return None
    body = types[0].child_by_field_name("body")
    if body is None or body.child_count == 0:
        return None
    last = body.children[-1]
    return last.start_byte if last.type == "}" else body.end_byte - 1


def apply_fixed_code_entry_ts(src: str, entry: dict) -> tuple:
    """tree-sitter variant of apply_fixed_code_entry. Same (new_src, info)
    contract; raises ValueError on schema/anchor problems (so the driver
    records a per-entry failure and the regex splicer can still try)."""
    parser = _ts_parser()
    if parser is None:
        raise RuntimeError("tree-sitter unavailable")

    info = {
        "file": entry.get("file"),
        "method": entry.get("method"),
        "operation": entry.get("operation") or "replace_method",
        "engine": "tree-sitter",
    }

    # Phase 1: explicit imports (string-level, identical to the regex splicer).
    imports = parse_imports_field(entry.get("imports"))
    src = add_imports(src, imports)
    info["imports_added"] = len(imports)

    operation = info["operation"]
    method_name = entry.get("method")
    code = entry.get("code") or ""
    if not method_name or not code:
        raise ValueError(f"missing method or code field: {entry}")

    src_bytes = src.encode("utf-8")
    tree = parser.parse(src_bytes)
    result_bytes = None

    if operation == "replace_method":
        node = _ts_find_method(tree, method_name, llm_code=code)
        if node is None:
            raise ValueError(
                f"replace_method: method {method_name!r} not found in {info['file']}")
        indent = _ts_indent_of(src_bytes, node.start_byte)
        line_start = src_bytes.rfind(b"\n", 0, node.start_byte) + 1
        new_code = reindent_block(code, indent)
        if not new_code.endswith("\n"):
            new_code += "\n"
        # Replace from column 0 of the method's first line (node.start_byte is
        # after the indent) so the reindented block isn't double-indented.
        result_bytes = (src_bytes[:line_start]
                        + new_code.encode("utf-8")
                        + src_bytes[node.end_byte:])

    elif operation == "insert_method":
        if _ts_find_method(tree, method_name) is not None:
            raise ValueError(
                f"insert_method: a method named {method_name!r} already exists "
                f"in {info['file']} — operation contradicts file state")
        anchor = parse_anchor(entry.get("anchor"))
        if anchor is None:
            info["anchor_warning"] = "missing or invalid anchor; defaulting to end_of_class"
            anchor = ("end_of_class", None)
        kind, target = anchor

        if kind in ("before_method", "after_method"):
            tnode = _ts_find_method(tree, target)
            if tnode is None:
                raise ValueError(
                    f"insert_method: anchor target {target!r} not found in {info['file']}")
            indent = _ts_indent_of(src_bytes, tnode.start_byte)
            new_code = reindent_block(code, indent)
            if kind == "before_method":
                line_start = src_bytes.rfind(b"\n", 0, tnode.start_byte) + 1
                result_bytes = (src_bytes[:line_start]
                                + new_code.encode("utf-8") + b"\n\n"
                                + src_bytes[line_start:])
            else:
                end = tnode.end_byte
                if src_bytes[end:end + 1] == b"\n":
                    end += 1
                result_bytes = (src_bytes[:end] + b"\n"
                                + new_code.encode("utf-8") + b"\n"
                                + src_bytes[end:])
        elif kind == "end_of_class":
            close = _ts_outer_class_close(tree)
            if close is None:
                raise ValueError(f"end_of_class: class body not found in {info['file']}")
            new_code = reindent_block(code, "    ")
            result_bytes = (src_bytes[:close] + b"\n"
                            + new_code.encode("utf-8") + b"\n"
                            + src_bytes[close:])

    if result_bytes is None:
        raise ValueError(f"unknown operation: {operation!r}")

    result_src = result_bytes.decode("utf-8", "replace")

    # Phase 3: auto-import inference (identical to the regex splicer).
    inferred = _infer_imports_for_inserted_code(result_src, code)
    if inferred:
        result_src = add_imports(result_src, inferred)
        info["imports_inferred"] = inferred

    return result_src, info


def apply_fixed_code_ts(flaky_root: Path, entries: list):
    """tree-sitter splice layer. Raises ImportError (with install hint) if
    tree-sitter / tree-sitter-java are not installed — see _ts_parser().

    All-or-nothing: snapshots every target file up front and rolls them ALL
    back if any entry fails. This is essential because the regex splice layer
    runs AFTER this one — a partial apply here would otherwise hand a dirtied
    tree to that fallback (e.g. an inserted method making its re-insert fail
    with 'already exists'). Mirrors apply_patch's transactional behaviour."""
    _ts_parser()   # raises with a clear install hint if the deps are missing
    if not entries:
        return {"layer": "splice tree-sitter", "ok": False,
                "reason": "no fixed_code entries"}

    # Snapshot all resolvable targets first, so a mid-batch failure rolls back.
    snap_targets = []
    for entry in entries:
        rel = entry.get("file")
        if rel:
            r = _resolve_path(flaky_root, rel)
            if r:
                snap_targets.append(r)
    snap = _snapshot(flaky_root, snap_targets)

    applied, failed = [], []
    for entry in entries:
        rel = entry.get("file")
        if not rel:
            failed.append({"entry": entry, "reason": "missing file field"})
            continue
        resolved = _resolve_path(flaky_root, rel)
        if resolved is None:
            failed.append({
                "entry": {k: v for k, v in entry.items() if k != "code"},
                "reason": f"file not found: {rel} (no unique suffix match in flaky tree)",
            })
            continue
        if resolved != rel:
            entry = dict(entry, file=resolved)
        target = flaky_root / resolved
        try:
            src = target.read_text(encoding="utf-8")
            new_src, info = apply_fixed_code_entry_ts(src, entry)
            target.write_text(new_src, encoding="utf-8")
            info["abs_path"] = str(target)
            if resolved != rel:
                info["path_resolved"] = {"original": rel, "resolved": resolved}
            applied.append(info)
        except Exception as e:
            failed.append({
                "entry": {k: v for k, v in entry.items() if k != "code"},
                "reason": str(e),
            })

    if failed:
        # Roll back any partial writes so the regex fallback starts clean.
        _rollback(flaky_root, snap)
        return {"layer": "splice tree-sitter", "ok": False,
                "applied": [], "failed": failed, "rolled_back": True}
    return {"layer": "splice tree-sitter", "ok": len(applied) > 0,
            "applied": applied, "failed": failed}


# ---------------------------------------------------------------------------
# Compile verification (smoke test, not full build)
# ---------------------------------------------------------------------------

def _which(cmd: str):
    r = subprocess.run(["which", cmd], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def _build_classpath(flaky_root: Path, m2_root: Path) -> str:
    parts = []
    for p in flaky_root.rglob("target/classes"):
        parts.append(str(p))
    for p in flaky_root.rglob("target/test-classes"):
        parts.append(str(p))
    if m2_root and m2_root.exists():
        for jar in m2_root.rglob("*.jar"):
            parts.append(str(jar))
    return ":".join(parts)


def verify_compile(flaky_root: Path, m2_root, touched_files: list) -> dict:
    """Compile each touched .java file with -d to a throwaway directory.
    This is a syntax/import smoke test, not a full build — we don't run tests."""
    javac = _which("javac")
    if javac is None:
        return {"skipped": True, "reason": "javac not on PATH"}

    java_files = [Path(f) for f in touched_files
                  if str(f).endswith(".java") and Path(f).exists()]
    if not java_files:
        return {"skipped": True, "reason": "no .java files touched"}

    cp = _build_classpath(flaky_root, m2_root) if m2_root else ""
    out_dir = "/tmp/applier_javac_out"
    os.makedirs(out_dir, exist_ok=True)

    results = []
    for jf in java_files:
        # The classpath (every jar under .m2) can exceed the OS single-argument
        # limit on Linux (E2BIG / "Argument list too long"); macOS allows far
        # larger args. Pass the options through a javac @argfile, which javac
        # reads from disk and so bypasses the OS arg-length limit entirely.
        argfile = None
        cmd = [javac, "-d", out_dir]
        if cp:
            fd, argfile = tempfile.mkstemp(suffix=".javac.args")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write('-cp "%s"\n' % cp.replace("\\", "\\\\"))
            cmd.append("@" + argfile)
        cmd.append(str(jf))
        try:
            r = subprocess.run(cmd, capture_output=True, text=True)
        finally:
            if argfile:
                os.unlink(argfile)
        rel = str(jf.relative_to(flaky_root)) if jf.is_relative_to(flaky_root) else str(jf)
        # Filter benign "annotation processor RELEASE_6" warnings — they're
        # noise from older deps and don't indicate a real problem.
        stderr = r.stderr or ""
        meaningful = "\n".join(
            ln for ln in stderr.splitlines()
            if "RELEASE_6" not in ln
            and "annotation processor" not in ln
            and "Annotation processing" not in ln
            and "-Xlint:-options" not in ln
            and "-proc:" not in ln
            and "(--processor-path" not in ln
            and not ln.strip().startswith("Use ")
        )
        results.append({
            "file": rel,
            "ok": r.returncode == 0,
            "stderr": meaningful.strip()[:2000],
        })

    return {
        "skipped": False,
        "all_ok": all(r["ok"] for r in results),
        "results": results,
    }


# ---------------------------------------------------------------------------
# Container recompile (regenerates target/test-classes)
# ---------------------------------------------------------------------------

# `mvn surefire:test` does NOT trigger compile/test-compile phases. After
# patching a test file in Flaky/, target/test-classes/ holds stale bytecode
# until something invokes test-compile explicitly. Surefire happily runs the
# old class — making every "did the LLM fix work?" check a false negative.
# We rebuild test-classes inside the container so the source/bytecode are
# in sync before any verification run.

def _module_for_file(flaky_root: Path, file_path: Path) -> str:
    """Walk up from `file_path` to the nearest pom.xml. Returns the module's
    relative path from `flaky_root`, or '.' for the root module.
    """
    flaky_resolved = flaky_root.resolve()
    p = file_path.resolve()
    if p.is_file():
        p = p.parent
    while True:
        if (p / "pom.xml").exists():
            try:
                return str(p.relative_to(flaky_resolved))
            except ValueError:
                return "."
        if p == flaky_resolved or p.parent == p:
            return "."
        p = p.parent


def _modules_to_recompile(flaky_root: Path, touched_files: list) -> list:
    """Collect unique Maven modules covering the touched files. If anything
    landed in the root module, return ['.'] so we recompile the whole tree.
    """
    modules = []
    seen = set()
    for f in touched_files:
        m = _module_for_file(flaky_root, f)
        if m not in seen:
            seen.add(m)
            modules.append(m)
    if "." in modules:
        return ["."]
    return modules


def _victim_module(container: str) -> str:
    """Return the victim test's Maven module from test_config.csv (the CSV
    'module' column), or "" if unavailable. Used so the recompile also builds
    the module the victim test lives in — which may differ from the module the
    fix landed in (abstract-superclass pattern: victim in ratis-test, fix in
    ratis-server). Best-effort: apply_fix.py must stay runnable standalone, so
    any failure here just falls back to touched-file modules only."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from assemble_llm_context import load_csv_row
        row = load_csv_row(container)
        return (row.get("module") or "").strip() if row else ""
    except Exception:
        return ""


def _container_running(container: str) -> tuple:
    r = subprocess.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return (False, f"container {container!r} not found")
    if r.stdout.strip() != "true":
        return (False, f"container {container!r} not running")
    return (True, None)


def recompile_in_container(container: str, modules: list) -> dict:
    """Run `mvn install -DskipTests` inside the docker container for the
    touched modules. This mirrors what the per-type pipelines' STEP 6c
    pre-build does, ensuring multi-phase build artifacts are available.

    Why `install -DskipTests`, not the narrower `test-compile`:
    Java projects with `package`-phase plugins (maven-shade-plugin to
    produce relocated/shaded jars, antrun, exec-maven-plugin generated
    sources, etc.) require those plugins to run BEFORE test sources can
    compile, because some test files reference the plugins' output.
    `mvn test-compile` only runs through the `test-compile` phase — it
    never reaches `package`, so the shaded jars don't exist on the
    classpath, and the test compile fails with cryptic "package X does
    not exist" errors on completely unrelated test files.

    Concrete example seen on BOOKKEEPER-846: `TestBackwardCompat.java`
    references `org.apache.bk_v4_2_0.bookkeeper.client` — a shaded
    backward-compat jar built by maven-shade-plugin during `package`.
    With `mvn test-compile` the shaded jar is missing, so test-compile
    of bookkeeper-server fails — even though the LLM's patch only
    touched a `@Test(timeout=...)` value on a different file.

    `mvn install -DskipTests` runs through the `install` lifecycle phase
    (which includes `package`), giving every plugin a chance to produce
    its outputs before test sources are compiled. `-DskipTests` skips
    only test EXECUTION — `test-compile` still runs as part of the
    lifecycle. Same recipe step 6c uses, same observed compile success.

    Cost: ~30-60s slower per recompile on large projects (BookKeeper).
    Acceptable trade-off for not silently false-failing on multi-phase
    projects.

        cd /app/work/Flaky && \\
        export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT && \\
        mvn install -DskipTests -pl <modules> -am \\
            -Dgpg.skip=true -Dcheckstyle.skip -Drat.skip \\
            -Denforcer.skip -Dmaven.javadoc.skip
    """
    if not modules:
        return {"skipped": True, "reason": "no modules to recompile"}

    if _which("docker") is None:
        return {"skipped": True, "reason": "docker not on PATH"}

    running, err = _container_running(container)
    if not running:
        return {"skipped": True, "reason": err}

    skip_flags = (
        "-Dgpg.skip=true -Dcheckstyle.skip -Drat.skip "
        "-Denforcer.skip -Dmaven.javadoc.skip"
    )
    if modules == ["."]:
        mvn_cmd = f"mvn install -DskipTests {skip_flags}"
    else:
        pl_arg = ",".join(modules)
        mvn_cmd = f"mvn install -DskipTests -pl {pl_arg} -am {skip_flags}"

    bash_cmd = (
        "cd /app/work/Flaky && "
        "export SUREFIRE_VERSION=3.0.0-M8-SNAPSHOT && "
        # Put the JDK Maven already uses on PATH so build plugins that spawn a
        # bare `java`/tool (exec-maven-plugin codegen, antrun, JavaCC/jute,
        # protoc) can find it. Guarded: expands to nothing if JAVA_HOME is
        # unset, and only prepends the JDK already in use — cannot break a
        # project that already had java on PATH.
        'export PATH="${JAVA_HOME:+$JAVA_HOME/bin:}$PATH" && '
        + mvn_cmd
    )

    r = subprocess.run(
        ["docker", "exec", container, "bash", "-lc", bash_cmd],
        capture_output=True, text=True,
    )
    return {
        "skipped": False,
        "ok": r.returncode == 0,
        "container": container,
        "modules": modules,
        "command": bash_cmd,
        "stderr_tail": (r.stderr or "")[-2000:],
        "stdout_tail": (r.stdout or "")[-2000:],
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def _touched_files_from_patch(flaky_root: Path, patch_text: str) -> list:
    files = []
    if not patch_text:
        return files
    for m in re.finditer(r'^\+\+\+\s+b/(.+)$', patch_text, re.M):
        rel = m.group(1).strip()
        # Resolve the same way fixed_code paths are (via _resolve_path): strip
        # a leading `Flaky/` and suffix-match into the tree. Without this, a
        # patch anchored at `Flaky/<module>/...` is joined onto a flaky_root
        # that already ends in `Flaky`, producing a non-existent double-`Flaky`
        # path that _module_for_file walks up to the ROOT pom -> '.' -> a
        # whole-reactor `mvn install` that drags in modules unrelated to the
        # victim (e.g. ratis-hadoop, whose protoc dep then fails) instead of
        # just the victim's module. Mirrors _touched_files_from_fixed_code.
        resolved = _resolve_path(flaky_root, rel) or rel
        files.append(flaky_root / resolved)
    return files


def _touched_files_from_fixed_code(flaky_root: Path, entries: list) -> list:
    """Return the absolute file paths the splicer would touch, applying the
    same path-fuzzy-match the splicer itself uses. Without this, an entry
    whose `file` field omits a Maven module prefix (the gpt-4o shardingsphere
    case) would surface here as a non-existent path, and the downstream
    compile/recompile steps would skip the actual modified file with
    'no .java files touched' — a misleading message that hides the real
    apply state from the verdict."""
    files = []
    for e in entries or []:
        rel = e.get("file")
        if not rel:
            continue
        resolved = _resolve_path(flaky_root, rel) or rel
        files.append(flaky_root / resolved)
    return files


def _save_report(base: Path, report: dict):
    out = base / "claude_outputs" / "apply_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    print(f"Report saved: {out}")


def _compute_verdict(report: dict) -> tuple:
    """Single source of truth for whether the apply pipeline as a whole
    succeeded. Called from BOTH _print_summary (for the human-readable
    RESULT: line) and main (for the process exit code), so they can never
    drift out of sync.

    Verdict rule:
      PASS iff (a) some layer landed the patch AND
              (b) the patched bytecode compiles.

    Bytecode-validity signal precedence:
      1. Container `mvn test-compile` if it ran (authoritative — uses
         Maven's real classpath construction; matches what downstream
         surefire reads).
      2. Host `javac` smoke test if container recompile didn't run.
         Brittle on real Maven projects but better than nothing.

    Returns (overall_ok: bool, msg: str, landed_layer: str|None).
    """
    landed = report.get("result") or {}
    landed_ok = bool(landed.get("ok"))
    landed_layer = landed.get("layer")

    rc = report.get("recompile") or {}
    recompile_ran = bool(rc) and not rc.get("skipped")

    if recompile_ran:
        bytecode_ok = bool(rc.get("ok"))
    else:
        c = report.get("compile") or {}
        bytecode_ok = bool(c) and (not c.get("skipped")) and bool(c.get("all_ok"))

    overall_ok = landed_ok and bytecode_ok

    if overall_ok:
        msg = f"PASS — applied via {landed_layer}, compiles cleanly"
    elif landed_ok:
        if recompile_ran:
            msg = f"FAIL — patch landed via {landed_layer}, but mvn test-compile failed (see above)"
        else:
            msg = f"FAIL — patch landed via {landed_layer}, but compile could not be confirmed (see above)"
    else:
        msg = f"FAIL — {landed.get('reason', 'no layer landed')}"

    return (overall_ok, msg, landed_layer)


def _print_summary(report: dict):
    """Binary [PASS]/[FAIL] reporting.

    The bytecode-validity verdict is driven by container `mvn test-compile`
    when available — it uses Maven's authoritative classpath construction
    and matches what downstream surefire actually reads. Host-side `javac`
    is brittle on real Maven projects (Lombok ↔ JDK module-system mismatch,
    classpath heuristics) and produces frequent false negatives even when
    the project compiles cleanly via Maven. So we print host-compile
    informationally with a `[INFO]` label when the container recompile
    already gave us the authoritative answer, and only let host-compile
    drive the verdict when the container recompile didn't run."""
    print()
    print("=" * 60)
    print(f"APPLY REPORT  container={report['container']}")
    print("=" * 60)
    for layer in report["layers_attempted"]:
        verdict = "[PASS]" if layer.get("ok") else "[FAIL]"
        name = layer.get("layer") or "(none)"
        detail = layer.get("reason") or ""
        if "applied" in layer:
            detail = f"{len(layer['applied'])} applied, {len(layer['failed'])} failed"
        print(f"  {verdict} {name:30s} {detail}")
        # Idea 1 diagnostic: which paths the fuzzy-matcher rewrote.
        if layer.get("path_rewritten"):
            for orig, new in layer["path_rewritten"].items():
                print(f"           + path rewritten: {orig} -> {new}")
        # Idea 1 diagnostic for the splicer: per-entry path resolutions.
        for ap in layer.get("applied", []) or []:
            if ap.get("path_resolved"):
                pr = ap["path_resolved"]
                print(f"           + path resolved: {pr['original']} -> {pr['resolved']}")
            # Idea 2 diagnostic: which imports auto-import inferred.
            if ap.get("imports_inferred"):
                imps = ", ".join(ap["imports_inferred"])
                print(f"           + inferred imports: {imps}")
        if layer.get("failed"):
            for f in layer["failed"]:
                print(f"           - {f['reason']}")

    rc = report.get("recompile") or {}
    recompile_ran = bool(rc) and not rc.get("skipped")
    recompile_ok = recompile_ran and bool(rc.get("ok"))

    if report.get("compile"):
        c = report["compile"]
        # Label: PASS/FAIL only when host compile is the verdict driver.
        # When the container recompile has already given us the authoritative
        # answer, host-compile is INFO — its result does not affect verdict.
        if c.get("skipped"):
            label = "[INFO]" if recompile_ran else "[FAIL]"
            print(f"  {label} compile (host javac): skipped ({c.get('reason')})")
        else:
            host_ok = c.get("all_ok")
            if recompile_ran:
                label = "[INFO]"  # always informational when authoritative recompile ran
            else:
                label = "[PASS]" if host_ok else "[FAIL]"
            n_ok = sum(1 for r in c["results"] if r["ok"])
            n_total = len(c["results"])
            suffix = "" if not recompile_ran else "  (informational; container recompile is authoritative)"
            print(f"  {label} compile (host javac): {n_ok}/{n_total} files OK{suffix}")
            for r in c["results"]:
                if not r["ok"]:
                    snippet = r["stderr"].splitlines()[0] if r["stderr"] else ""
                    print(f"           - {r['file']}: {snippet}")

    if report.get("recompile"):
        if rc.get("skipped"):
            print(f"  [FAIL] recompile: skipped ({rc.get('reason')})")
        else:
            verdict = "[PASS]" if rc.get("ok") else "[FAIL]"
            mods = ",".join(rc.get("modules", [])) or "(none)"
            print(f"  {verdict} recompile: mvn test-compile -pl {mods}")
            if not rc.get("ok"):
                tail = (rc.get("stderr_tail") or rc.get("stdout_tail") or "")
                for ln in tail.splitlines()[-5:]:
                    print(f"           {ln}")

    _, msg, _ = _compute_verdict(report)
    print()
    print(f"RESULT: {msg}")


def main():
    parser = argparse.ArgumentParser(description="Apply LLM fix to Flaky/ tree")
    parser.add_argument("container", help="result_container directory name")
    parser.add_argument("--no-verify", action="store_true",
                        help="skip javac compile verification")
    parser.add_argument("--no-recompile", action="store_true",
                        help="skip container-side mvn test-compile")
    parser.add_argument("--docker-container",
                        help="docker container name (default: tm_<container>)")
    parser.add_argument("--dry-run", action="store_true",
                        help="check only; don't modify files")
    args = parser.parse_args()

    base = container_run_dir(args.container)
    if not base.is_dir():
        sys.exit(f"ERROR: {base} not found")

    flaky = base / "Flaky"
    if not flaky.is_dir():
        sys.exit(f"ERROR: {flaky} not found")

    response_path = base / "claude_outputs" / "llm_response.json"
    if not response_path.is_file():
        sys.exit(f"ERROR: {response_path} not found")

    response = json.loads(response_path.read_text(encoding="utf-8"))
    output_a_patch = (response.get("response", {})
                              .get("output_a", {})
                              .get("patch"))
    fixed_code = (response.get("response", {})
                          .get("output_b", {})
                          .get("fixed_code", []))

    report = {
        "container": args.container,
        "flaky_root": str(flaky),
        "dry_run": args.dry_run,
        "layers_attempted": [],
        "result": None,
    }

    landed = None

    # ---- Layer 1 & 2: output_a patch ----
    if output_a_patch:
        if args.dry_run:
            r = check_patch(flaky, output_a_patch)
            r["layer"] = (r.get("layer") or "output_a") + " (dry-run)"
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r
        else:
            r = apply_patch(flaky, output_a_patch)
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r
    else:
        report["layers_attempted"].append(
            {"layer": "output_a", "ok": False, "reason": "no patch in response"})

    # ---- Layer 3: tree-sitter structured splice (preferred over regex) ----
    # Consumes the same output_b.fixed_code as Layer 4 but locates methods /
    # class-close with a real Java parser. REQUIRED: raises ImportError with
    # an install hint if tree-sitter / tree-sitter-java aren't present. We
    # deliberately do NOT silently fall through — silent absence would hide
    # environmental drift between machines and produce inconsistent reports.
    # Layer 4 (regex) remains as the fallback for cases this layer DEFERS
    # (e.g. ambiguous end_of_class in a multi-top-level-type file).
    if landed is None and fixed_code and not args.dry_run:
        r = apply_fixed_code_ts(flaky, fixed_code)
        report["layers_attempted"].append(r)
        if r.get("ok"):
            landed = r

    # ---- Layer 4: regex splice output_b.fixed_code (fallback) ----
    if landed is None and fixed_code:
        if args.dry_run:
            report["layers_attempted"].append({
                "layer": "splice output_b (dry-run)",
                "ok": True,
                "reason": f"would splice {len(fixed_code)} entr"
                          f"{'y' if len(fixed_code) == 1 else 'ies'}",
            })
        else:
            r = apply_fixed_code(flaky, fixed_code)
            report["layers_attempted"].append(r)
            if r.get("ok"):
                landed = r

    # ---- Compile verification + container recompile ----
    if landed and not args.dry_run:
        touched = _touched_files_from_patch(flaky, output_a_patch) + \
                  _touched_files_from_fixed_code(flaky, fixed_code)
        # Dedup while preserving order
        seen = set()
        deduped = []
        for f in touched:
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                deduped.append(f)

        if not args.no_verify:
            m2 = base / "Flakym2" / ".m2" / "repository"
            report["compile"] = verify_compile(flaky, m2 if m2.exists() else None, deduped)

        if not args.no_recompile:
            # Match the sanitization done by run_{td,od}_tracemop.sh
            #   CONTAINER="tm_${RESULT_CONTAINER//[^a-zA-Z0-9]/_}"
            # so containers like "BOOKKEEPER-846" -> "tm_BOOKKEEPER_846"
            # are findable when apply_fix.py is invoked standalone.
            sanitized = re.sub(r'[^a-zA-Z0-9]', '_', args.container)
            container_name = args.docker_container or f"tm_{sanitized}"
            modules = _modules_to_recompile(flaky, deduped)
            # Also recompile the victim's own module so verify (which runs
            # `surefire:test -pl <victim-module>`) has fresh test-classes. The
            # fix may land in an upstream module (e.g. ratis-server) while the
            # victim test lives in another (ratis-test); without this the victim
            # module is never compiled and verify reports "No tests to run".
            # `-am` still pulls only upstream deps, so unrelated sibling modules
            # (e.g. ratis-hadoop -> protoc) are not built. Skipped when the set
            # is already the whole tree (["."]).
            victim_module = _victim_module(args.container)
            if (victim_module and victim_module != "."
                    and modules != ["."] and victim_module not in modules):
                modules.append(victim_module)
            report["recompile"] = recompile_in_container(container_name, modules)

    report["result"] = landed or {
        "layer": None,
        "ok": False,
        "reason": "no layer landed the fix",
    }

    _save_report(base, report)
    _print_summary(report)

    # Exit code is driven by the same verdict logic the summary printed —
    # see _compute_verdict for the full rule. Keeping a single source of
    # truth prevents the printed RESULT and the shell exit code from
    # disagreeing if the verdict logic changes later.
    overall_ok, _, _ = _compute_verdict(report)
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
