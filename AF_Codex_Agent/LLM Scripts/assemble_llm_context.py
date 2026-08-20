#!/usr/bin/env python3
"""
assemble_llm_context.py — shared helpers for the per-type assemblers.

This module is NOT a runnable assembler on its own. It exposes the pure
data-extraction helpers that every per-type entry point reuses:

    assemble_llm_context_od.py   (OD / brittle — has a polluter)
    assemble_llm_context_td.py   (TD — no polluter)
    assemble_llm_context_id.py   (ID — NonDex iteration-order shuffle)
    assemble_llm_context_nio.py  (NIO — non-idempotent self-pollution)

Each per-type script imports the helpers below, then assembles its own
sectioning, TASK framing, and TWO-TURN PROTOCOL inline (the protocol blocks
are deliberately duplicated per type so a future change to one test type's
prompt cannot silently shift another type's behavior).
"""

import csv
import glob
import os
import re


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Script lives in AF_Codex_Agent/LLM Scripts/ ; data and CSV are one level up.
DATA_DIR = os.path.join(SCRIPT_DIR, "..", "data")
CSV_FILE = os.path.join(SCRIPT_DIR, "..", "test_config.csv")


# ---------------------------------------------------------------------------
# CSV helpers
# ---------------------------------------------------------------------------

def load_csv_row(result_container):
    with open(CSV_FILE, encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row.get("result_container") or "").strip() == result_container:
                return row
    return None


# ---------------------------------------------------------------------------
# File reading helpers
# ---------------------------------------------------------------------------

def read_file_safe(path, encoding="utf-8"):
    """Read a file, return empty string if missing."""
    if not os.path.isfile(path):
        return ""
    for enc in (encoding, "utf-8-sig", "utf-16-le", "utf-16", "latin-1"):
        try:
            with open(path, encoding=enc) as f:
                content = f.read()
            if "\x00" in content and enc == "latin-1":
                continue
            return content
        except (UnicodeDecodeError, UnicodeError):
            continue
    return ""


# ---------------------------------------------------------------------------
# Source code extraction
# ---------------------------------------------------------------------------

def fqn_to_path(fqn_with_method):
    """
    Convert 'com.j256.ormlite.logger.LoggerFactoryTest#testSetLogFactory'
    into ('com/j256/ormlite/logger/LoggerFactoryTest.java', 'testSetLogFactory')

    Handles inner classes named with '$' (Foo$Bar → Foo) and with '.'
    (Foo.Bar → Foo): both resolve to the top-level class file. The agent and
    Java stack traces / `extends` clauses use dot notation for nested classes,
    so 'pkg.Outer.Inner' must map to 'pkg/Outer.java', not 'pkg/Outer/Inner.java'.
    """
    if "#" in fqn_with_method:
        class_fqn, method = fqn_with_method.rsplit("#", 1)
    else:
        class_fqn = fqn_with_method
        method = None

    # Strip inner class (e.g., Foo$Bar → Foo)
    if "$" in class_fqn:
        class_fqn = class_fqn[:class_fqn.index("$")]

    # Collapse dotted nested classes: by Java convention package segments are
    # lowercase and the top-level class is the first uppercase-initial segment;
    # any segments after it are nested classes living in that same .java file.
    parts = class_fqn.split(".")
    for i, p in enumerate(parts):
        if p[:1].isupper():
            parts = parts[:i + 1]
            break

    rel_path = "/".join(parts) + ".java"
    return rel_path, method


def find_source_file(base_dir, module, rel_path, search_dirs=("src/test/java", "src/main/java")):
    """
    Find a Java source file under the project. Tries:
      1. <base>/Flaky/<module>/<search_dir>/<rel_path>  (multi-module)
      2. <base>/Flaky/<search_dir>/<rel_path>           (single module)
      3. <base>/Flaky/*/<search_dir>/<rel_path>         (any sibling module)
    Step 3 handles classes that live outside the victim's module — e.g. an
    abstract test superclass or shared base test in a different module
    (the `TestXWithGrpc extends XTests` idiom), or production code in a
    -core/-common module the victim depends on. It only fires when the
    targeted lookups miss, so it never changes an existing successful match.
    """
    for search_dir in search_dirs:
        # Multi-module path
        if module and module != ".":
            candidate = os.path.join(base_dir, "Flaky", module, search_dir, rel_path)
            if os.path.isfile(candidate):
                return candidate

        # Single-module path
        candidate = os.path.join(base_dir, "Flaky", search_dir, rel_path)
        if os.path.isfile(candidate):
            return candidate

    # Cross-module fallback: same rel_path under ANY module's search_dir.
    # Sorted for deterministic selection if the path exists in >1 module.
    for search_dir in search_dirs:
        hits = sorted(glob.glob(
            os.path.join(base_dir, "Flaky", "*", search_dir, rel_path)))
        if hits:
            return hits[0]

    return None


def _code_mask(src):
    r"""Return a copy of `src` with the *contents* of string/char literals and
    comments replaced by spaces (newlines preserved), leaving real code —
    including braces — intact. Length and offsets are preserved.

    This lets brace/structure scanning ignore braces inside literals/comments
    (e.g. a malformed-JSON fixture like "{\"k\":\"v\"" whose unmatched '{'
    would otherwise unbalance the count). Mirrors apply_fix._code_mask; kept
    self-contained here to avoid coupling this base module to the applier.
    Java text blocks (\"\"\" ... \"\"\", Java 15+) are not special-cased.
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
        # STR or CHAR: handle escapes so an escaped quote doesn't close it.
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


# Keywords that can precede `name(` in a call/expression context — used to
# tell a method *declaration* (`void name(`) apart from an *invocation*
# (`return name(`, `new name(`, a bare `name(...)` statement, etc.).
_CALL_PRECEDING_KEYWORDS = {
    "return", "new", "throw", "yield", "assert", "else", "do", "while", "if",
}


def _looks_like_method_decl(masked_line, method_name):
    """Heuristic: does `method_name(` on this masked line look like a method
    declaration (a return type / modifier precedes the name) rather than a
    call site (statement start, or after '.', '=', ',', '(', 'return', ...)?

    Stops extract_java_method from anchoring on an earlier *call* to the method
    (very common: a test helper invoked above its own declaration) and then
    brace-counting from the wrong place, which truncates the slice.
    """
    m = re.search(r'\b' + re.escape(method_name) + r'\s*\(', masked_line)
    if not m:
        return False
    prefix = masked_line[:m.start()].rstrip()
    if not prefix:
        return False                       # name at statement start → call
    if not re.match(r'[\w$>\]]', prefix[-1]):
        return False                       # preceded by '.', '=', ',', '(' …
    return prefix.split()[-1] not in _CALL_PRECEDING_KEYWORDS


def extract_java_method(file_path, method_name, target_line=None,
                        all_overloads=False):
    """
    Extract a method from a Java file by matching the method signature
    and tracking brace depth to find the end.

    If target_line is provided (1-indexed), picks the overload whose body
    contains that line. Otherwise picks the first match — unless
    all_overloads=True, in which case EVERY matching declaration is returned
    (concatenated, separated by a marker). A bare method name routinely matches
    more than one declaration — e.g. a public no-arg `@Test` wrapper plus a
    private overload that holds the real logic and assertions — and emitting
    only the first leaves the caller blind to the body it actually needs.

    Brace depth and the signature search run over a string/comment-masked
    view so a brace (or a method-name-like token) inside a literal or comment
    can't mis-bound the method.

    Returns the method source code as a string, or None.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()

    # Masked view for structure scanning; same length/line-count as `lines`
    # so indices line up 1:1. Fall back to raw lines if anything is off.
    masked_lines = _code_mask("".join(lines)).splitlines(keepends=True)
    if len(masked_lines) != len(lines):
        masked_lines = lines

    # Find ALL occurrences of method_name( on the masked view, separating real
    # declarations from call sites so we never anchor on an invocation.
    decl_candidates = []
    all_candidates = []
    for i, line in enumerate(masked_lines):
        if re.search(r'\b' + re.escape(method_name) + r'\s*\(', line):
            # Walk backwards to include annotations
            start = i
            while start > 0 and lines[start - 1].strip().startswith("@"):
                start -= 1
            all_candidates.append(start)
            if _looks_like_method_decl(line, method_name):
                decl_candidates.append(start)

    # Prefer real declarations; fall back to any match (incl. call sites) so
    # behaviour is unchanged when no declaration-shaped line is found.
    candidates = decl_candidates or all_candidates
    if not candidates:
        return None

    def _slice_from(method_start):
        # Track brace depth from the opening { to find the method end.
        brace_depth = 0
        found_open = False
        method_end = None
        for i in range(method_start, len(lines)):
            for ch in masked_lines[i]:
                if ch == '{':
                    brace_depth += 1
                    found_open = True
                elif ch == '}':
                    brace_depth -= 1
                    if found_open and brace_depth == 0:
                        method_end = i
                        break
            if method_end is not None:
                break
        if method_end is None:
            # Fallback: return 30 lines from method start
            method_end = min(method_start + 30, len(lines) - 1)
        return "".join(lines[method_start:method_end + 1])

    # Emit every overload when asked and no specific line is targeted.
    if all_overloads and target_line is None and len(candidates) > 1:
        seen = set()
        uniq = [c for c in candidates if not (c in seen or seen.add(c))]
        return "\n// ---- next overload ----\n".join(
            _slice_from(c) for c in uniq)

    # If target_line given and multiple overloads, find the one containing that
    # line (target_line is 1-indexed, list is 0-indexed). Otherwise first match.
    method_start = candidates[0]
    if target_line and len(candidates) > 1:
        target_idx = int(target_line) - 1
        best = candidates[0]
        for c in candidates:
            if c <= target_idx:
                best = c
            else:
                break
        method_start = best

    return _slice_from(method_start)


# ---------------------------------------------------------------------------
# Class-header (structural) extraction
# ---------------------------------------------------------------------------

def _strip_java_strings_and_line_comments(line):
    """Return `line` with string/char literals and `//` line comments
    replaced by spaces, so brace counting isn't fooled by braces in those
    contexts. Block comments spanning lines are not handled (rare in test
    code; accepted imperfection)."""
    out = []
    i = 0
    n = len(line)
    while i < n:
        ch = line[i]
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break  # rest of line is a line comment
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            j = line.find("*/", i + 2)
            if j == -1:
                break  # block comment continues past line — drop the rest
            out.append(" " * (j + 2 - i))
            i = j + 2
            continue
        if ch == '"':
            j = i + 1
            while j < n and line[j] != '"':
                j += 2 if line[j] == "\\" and j + 1 < n else 1
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue
        if ch == "'":
            j = i + 1
            while j < n and line[j] != "'":
                j += 2 if line[j] == "\\" and j + 1 < n else 1
            out.append(" " * (j + 1 - i))
            i = j + 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


# Detect inner-class declarations on a single class-scope line.
_INNER_CLASS_RE = re.compile(
    r"^\s*(?:(?:public|private|protected|static|final|abstract|sealed|non-sealed|strictfp)\s+)*"
    r"(?:class|interface|enum|record|@interface)\s+\w+"
)


# Java declaration keywords. If any appear on a line that also starts with
# `@`, the line is an inline-annotated declaration (e.g. `@Test public void
# foo() {`), NOT a pure annotation line — do not consume it during lookahead.
# (?<!\.) excludes member references like `Suite.class`, `Foo.interface`,
# where the keyword is part of a `.class`/etc. reference inside an annotation
# argument — those are NOT declarations.
_DECL_KEYWORD_RE = re.compile(
    r"(?<!\.)\b(public|private|protected|void|static|final|abstract|"
    r"synchronized|native|default|class|interface|enum|record|byte|short|"
    r"int|long|float|double|boolean|char)\b"
)


def _skip_annotation_lines(lines, start, scope_depth, depths):
    """Walk forward from `start` consuming PURE annotation lines (lines whose
    only content at class scope is one or more `@Foo(...)` expressions).
    Multi-line `@Foo(\\n...\\n)` annotations are followed by paren-tracking.

    Lines that combine an annotation with a declaration on the same line
    (e.g. `@Test public void foo() {`) are NOT consumed — they are returned
    so the caller's method/class-detection branch handles them.

    Returns the index of the first non-pure-annotation line at scope_depth."""
    i = start
    n = len(lines)
    while i < n and depths[i] == scope_depth:
        analysis = _strip_java_strings_and_line_comments(lines[i]).strip()
        if not analysis.startswith("@"):
            return i
        # Inline-annotation-on-declaration check: if this line also contains a
        # Java declaration keyword, treat it as a declaration line, not an
        # annotation line.
        if _DECL_KEYWORD_RE.search(analysis):
            return i
        # Pure-annotation line. Consume it; track parens for multi-line forms.
        depth_paren = 0
        for ch in analysis:
            if ch == "(":
                depth_paren += 1
            elif ch == ")":
                depth_paren -= 1
        i += 1
        while i < n and depth_paren > 0 and depths[i] == scope_depth:
            content = _strip_java_strings_and_line_comments(lines[i])
            for ch in content:
                if ch == "(":
                    depth_paren += 1
                elif ch == ")":
                    depth_paren -= 1
            i += 1
    return i


def _process_class_body(lines, start_idx, scope_depth, depths, include_inner):
    """Recursive worker for extract_class_header. Walk class-body lines at
    `scope_depth`, emit class-scope content (fields, comments, annotations,
    method signatures, inner-class declarations). Skip method bodies. Recurse
    into inner classes when include_inner is True."""
    out = []
    i = start_idx
    n = len(lines)

    while i < n and depths[i] >= scope_depth:
        if depths[i] > scope_depth:
            i += 1
            continue

        line = lines[i]
        analysis = _strip_java_strings_and_line_comments(line).strip()

        # Static initializer block — collapse to one line.
        # Heuristic: starts with `static`, has `{`, no `(` before the `{`.
        if (
            analysis.startswith("static")
            and "{" in analysis
            and "(" not in analysis.split("{", 1)[0]
        ):
            out.append(" " * (4 * scope_depth) + "static { /* ... */ }\n")
            i += 1
            while i < n and depths[i] > scope_depth:
                i += 1
            continue

        # Look ahead past any annotation lines to find the actual declaration.
        la = _skip_annotation_lines(lines, i, scope_depth, depths)
        if la >= n or depths[la] != scope_depth:
            # No declaration follows on this scope — emit lines verbatim and advance.
            end = max(la, i + 1)
            for k in range(i, end):
                if k < n:
                    out.append(lines[k])
            i = end
            continue

        la_analysis = _strip_java_strings_and_line_comments(lines[la]).strip()

        # Inner-class declaration?
        if _INNER_CLASS_RE.match(la_analysis):
            # Find the line containing the opening `{`
            la2 = la
            while la2 < n and "{" not in _strip_java_strings_and_line_comments(lines[la2]):
                la2 += 1
            if la2 >= n:
                # Declaration without an opening brace — emit and stop.
                for k in range(i, la + 1):
                    out.append(lines[k])
                i = la + 1
                continue
            # Emit i..la2 inclusive (annotations + class header + opening `{`)
            for k in range(i, la2 + 1):
                out.append(lines[k])
            j = la2 + 1
            if include_inner:
                # Recursion processes the inner class body at scope_depth+1.
                # When it returns, j points to the line AFTER the inner class's
                # closing `}` — recursion has already emitted that `}` itself.
                inner_text, j = _process_class_body(
                    lines, j, scope_depth + 1, depths, include_inner
                )
                out.append(inner_text)
            else:
                out.append(
                    " " * (4 * (scope_depth + 1))
                    + "// ... inner class body omitted ...\n"
                )
                # Skip body lines (depths > scope_depth). After the loop,
                # lines[j-1] is the inner class's closing `}` line (if any
                # body iterations ran), so emit it for symmetry.
                while j < n and depths[j] > scope_depth:
                    j += 1
                if j > la2 + 1 and j - 1 < n and "}" in lines[j - 1]:
                    out.append(lines[j - 1])
            i = j
            continue

        # Method/constructor declaration? (la_analysis contains `(`)
        if "(" in la_analysis:
            la2 = la
            found_brace_at = None
            found_semi_at = None
            while la2 < n and depths[la2] == scope_depth:
                content = _strip_java_strings_and_line_comments(lines[la2])
                if "{" in content:
                    found_brace_at = la2
                    break
                if ";" in content:
                    found_semi_at = la2
                    break
                la2 += 1

            if found_brace_at is not None:
                # Emit i..found_brace_at-1 verbatim, then signature with `;`
                for k in range(i, found_brace_at):
                    out.append(lines[k])
                sig = lines[found_brace_at][
                    : lines[found_brace_at].index("{")
                ].rstrip()
                out.append(sig + ";\n")
                j = found_brace_at + 1
                while j < n and depths[j] > scope_depth:
                    j += 1
                i = j
                continue
            if found_semi_at is not None:
                # Abstract/interface method — emit verbatim
                for k in range(i, found_semi_at + 1):
                    out.append(lines[k])
                i = found_semi_at + 1
                continue
            # Couldn't find brace/semi — emit collected lines and advance.
            for k in range(i, la2):
                out.append(lines[k])
            i = la2
            continue

        # Default: field declaration, plain comment, blank line — emit verbatim.
        out.append(line)
        i += 1

    return "".join(out), i


def extract_class_header(file_path, include_inner_classes=False, max_lines=400):
    """Return the structural header of a Java file: package + imports +
    class signature(s) + field declarations + method signatures (no method
    bodies). Inner classes are processed structurally when
    include_inner_classes is True; otherwise they appear as a declaration line
    plus a `// ... inner class body omitted ...` placeholder.

    Returns None if file_path is missing.
    Caps output at max_lines, with a trailing truncation marker.
    """
    if not file_path or not os.path.isfile(file_path):
        return None

    with open(file_path, encoding="utf-8") as f:
        lines = f.readlines()

    # Compute brace depth at the start of each line.
    depths = []
    d = 0
    for line in lines:
        depths.append(d)
        for ch in _strip_java_strings_and_line_comments(line):
            if ch == "{":
                d += 1
            elif ch == "}":
                d -= 1

    n = len(lines)
    out = []
    i = 0
    while i < n:
        # Pre-class content (depth 0): emit verbatim
        while i < n and depths[i] == 0:
            out.append(lines[i])
            i += 1
        if i < n:
            body, new_i = _process_class_body(lines, i, 1, depths, include_inner_classes)
            out.append(body)
            i = new_i

    text = "".join(out)
    text_lines = text.splitlines(keepends=True)
    if len(text_lines) > max_lines:
        text = "".join(text_lines[:max_lines]) + (
            f"... ({len(text_lines) - max_lines} more header lines truncated)\n"
        )
    return text


# ---------------------------------------------------------------------------
# Failure output extraction
# ---------------------------------------------------------------------------

# A thrown exception/error printed by Surefire starts with a fully-qualified
# class name ending in Exception/Error/Throwable/Failure, e.g.
# "java.lang.AssertionError:", "org.opentest4j.AssertionFailedError:",
# "com.example.MyCustomException:". This matches any package (not just
# java.lang/org.*) so non-JDK exceptions are not missed.
_EXCEPTION_LINE_RE = re.compile(
    r'^(?:[a-zA-Z_$][\w$]*\.)+[A-Z][\w$]*'
    r'(?:Exception|Error|Throwable|Failure)\b'
)


def extract_failure_from_log(log_path):
    """
    Extract the first test failure block from a Maven/Surefire log.
    Captures: the FAILURE line, the exception, the full stack trace,
    and the error message (which may appear after a blank line).
    """
    content = read_file_safe(log_path)
    if not content:
        return "(no log file found)"

    lines = content.splitlines()

    # Strategy: find the thrown-exception line (e.g.
    # "org.opentest4j.AssertionFailedError: ..."), then capture everything
    # until we reach the Surefire results summary.
    exc_start = None
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.startswith("at ") and _EXCEPTION_LINE_RE.match(stripped):
            exc_start = i
            break

    if exc_start is None:
        # Fallback: find <<< FAILURE! line
        for i, line in enumerate(lines):
            if "<<< FAILURE!" in line or "<<< ERROR!" in line:
                exc_start = i
                break

    if exc_start is None:
        # Last resort: Tests run: summary
        for line in lines:
            if "Tests run:" in line and "Failures:" in line:
                return line.strip()
        return "(no failure output found in log)"

    # Include 2 context lines before the exception
    start = max(0, exc_start - 2)
    failure_lines = []

    for i in range(start, len(lines)):
        line = lines[i]
        stripped = line.strip()

        # Stop at results summary or next test section
        if stripped.startswith("[INFO] Results:"):
            break
        if stripped.startswith("[ERROR] Failures:"):
            # Include this summary line then stop
            failure_lines.append(line)
            break

        failure_lines.append(line)

        # Safety cap: keep the full exception + stack trace (deep traces with
        # "Caused by:" chains routinely exceed 40 lines); the boundary checks
        # above are the normal terminators, this only guards pathological logs.
        if len(failure_lines) > 300:
            break

    if not failure_lines:
        return "(no failure output found in log)"

    return "\n".join(failure_lines).strip()


# ---------------------------------------------------------------------------
# Stack trace → production code extraction
# ---------------------------------------------------------------------------

def extract_production_code_from_stacktrace(failure_text, base_dir, module, project_package):
    """
    Parse the stack trace for project-package classes under src/main/java,
    extract the relevant methods.

    project_package: e.g., "com.j256.ormlite" — derived from victim FQN.
    """
    if not failure_text or not project_package:
        return []

    # Find all "at com.j256.ormlite.something.Class.method(File.java:NNN)" lines
    pattern = re.compile(
        r'at\s+(' + re.escape(project_package) + r'\.[A-Za-z0-9_$.]+)\.(\w+)\((\w+\.java):(\d+)\)'
    )

    seen = set()
    results = []

    for match in pattern.finditer(failure_text):
        class_fqn = match.group(1)
        method_name = match.group(2)
        filename = match.group(3)
        line_no = match.group(4)

        key = f"{class_fqn}#{method_name}"
        if key in seen:
            continue
        seen.add(key)

        # Try to find in src/main/java (production code)
        rel_path, _ = fqn_to_path(f"{class_fqn}#dummy")
        source_file = find_source_file(
            base_dir, module, rel_path,
            search_dirs=("src/main/java",)
        )

        if source_file:
            method_src = extract_java_method(source_file, method_name, target_line=line_no)
            if method_src:
                results.append({
                    "class": class_fqn,
                    "method": method_name,
                    "file": filename,
                    "line": line_no,
                    "source": method_src,
                })

    return results


def derive_project_package(fqn, depth=3):
    """
    Derive the project base package from a FQN.
    'com.j256.ormlite.table.SchemaUtilsTest#testCreateSchemaDao' → 'com.j256.ormlite'
    """
    class_part = fqn.split("#")[0] if "#" in fqn else fqn
    parts = class_part.split(".")
    if len(parts) >= depth:
        return ".".join(parts[:depth])
    return class_part

