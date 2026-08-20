#!/usr/bin/env python3
"""Semantic, reference-compatible preparation for TD verification.

The TD dataset supplies four logically related trees::

    B   pristine (``Flaky``)
    P   pristine plus the timing perturbation (``FlakyCodeChange``)
    F   developer-fixed (``Fixed``)
    FP  developer-fixed plus the perturbation (``FixedCodeChange``)

The old scorer only considered ``B -> P``.  A genuine repair can remove the
line that this textual patch uses as an anchor, even though ``F -> FP`` proves
that the perturbation has a meaningful location after the repair.  This module
therefore derives both forcing contexts and applies the *reference-fixed*
context to a private copy of a candidate.  Expected overlap, a missing semantic
anchor, or an infrastructure problem is reported as ``INCOMPLETE``; it is never
turned into a model ``FAILED`` verdict here.

This file deliberately contains no Claude, Docker, Maven, or result-reporting
logic.  The driver can integrate it without giving the agent access to B/P/F/FP
or to the resulting manifest.  Calibration execution is delegated to an
injected runner so the protected evaluator can choose Docker, local processes,
or a remote worker.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shutil
import subprocess
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Protocol, Sequence


MANIFEST_SCHEMA = "agentflake.td-oracle/v1"
IGNORED_DIRS = frozenset({".git", "target", ".gradle", ".idea"})
IGNORED_FILES = frozenset({
    ".DS_Store", ".flattened-pom.xml", "dependency-reduced-pom.xml",
    "pom.xml.versionsBackup",
})
SOURCE_SUFFIXES = frozenset({
    ".java", ".kt", ".kts", ".groovy", ".scala", ".xml", ".properties",
    ".gradle", ".sh", ".py",
})
SOURCE_FILENAMES = frozenset({"pom.xml", "build.gradle", "settings.gradle"})


class OracleDisposition(str, Enum):
    """Whether an evaluator can soundly proceed.

    These are intentionally not model verdicts.  In particular, an overlap is
    ``INCOMPLETE`` rather than ``FAILED``.
    """

    PASSABLE = "PASSABLE"
    INCOMPLETE = "INCOMPLETE"


@dataclass(frozen=True)
class OracleOutcome:
    disposition: OracleDisposition
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def passable(self) -> bool:
        return self.disposition is OracleDisposition.PASSABLE

    @classmethod
    def ready(cls, code: str, message: str, **details: Any) -> "OracleOutcome":
        return cls(OracleDisposition.PASSABLE, code, message, details)

    @classmethod
    def incomplete(cls, code: str, message: str, **details: Any) -> "OracleOutcome":
        return cls(OracleDisposition.INCOMPLETE, code, message, details)


class ChangeKind(str, Enum):
    ADD = "ADD"
    MODIFY = "MODIFY"
    DELETE = "DELETE"


@dataclass(frozen=True)
class FileRecord:
    path: str
    sha256: str
    size: int
    kind: str = "file"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "kind": self.kind,
        }


@dataclass(frozen=True)
class TreeManifest:
    label: str
    digest: str
    files: tuple[FileRecord, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "digest": self.digest,
            "files": [record.to_dict() for record in self.files],
        }


@dataclass(frozen=True)
class TextHunk:
    """One exact line-oriented change plus stable surrounding context."""

    old: tuple[str, ...]
    new: tuple[str, ...]
    before: tuple[str, ...]
    after: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "old": list(self.old),
            "new": list(self.new),
            "before": list(self.before),
            "after": list(self.after),
        }


@dataclass(frozen=True)
class FileDelta:
    path: str
    kind: ChangeKind
    base_sha256: Optional[str]
    forced_sha256: Optional[str]
    hunks: tuple[TextHunk, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "kind": self.kind.value,
            "base_sha256": self.base_sha256,
            "forced_sha256": self.forced_sha256,
            "hunks": [hunk.to_dict() for hunk in self.hunks],
        }


@dataclass(frozen=True)
class ForcingDelta:
    """A source-only perturbation derived in one tree context."""

    context: str
    base_root: Path = field(repr=False)
    forced_root: Path = field(repr=False)
    files: tuple[FileDelta, ...]
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "context": self.context,
            "digest": self.digest,
            "files": [delta.to_dict() for delta in self.files],
        }


@dataclass(frozen=True)
class ProtectedTrees:
    pristine: Path
    perturbed: Path
    fixed: Path
    fixed_perturbed: Path

    def labelled(self) -> tuple[tuple[str, Path], ...]:
        return (
            ("pristine", self.pristine),
            ("perturbed", self.perturbed),
            ("fixed", self.fixed),
            ("fixed_perturbed", self.fixed_perturbed),
        )


@dataclass(frozen=True)
class OracleManifest:
    schema: str
    trees: tuple[TreeManifest, ...]
    pristine_forcing_digest: str
    reference_forcing_digest: str
    digest: str

    def payload(self, include_digest: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": self.schema,
            "trees": [tree.to_dict() for tree in self.trees],
            "forcing": {
                "pristine": self.pristine_forcing_digest,
                "reference": self.reference_forcing_digest,
            },
        }
        if include_digest:
            payload["digest"] = self.digest
        return payload

    def to_json(self) -> str:
        return json.dumps(self.payload(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class TDOracle:
    trees: ProtectedTrees
    pristine_forcing: ForcingDelta
    reference_forcing: ForcingDelta
    manifest: OracleManifest

    def verify_protected_trees(self) -> OracleOutcome:
        try:
            current = tuple(
                build_tree_manifest(path, label)
                for label, path in self.trees.labelled()
            )
        except OSError as exc:
            return OracleOutcome.incomplete(
                "PROTECTED_TREE_READ_ERROR",
                "A protected TD oracle tree could not be hashed.",
                error=str(exc),
                manifest_digest=self.manifest.digest,
            )
        expected = {tree.label: tree.digest for tree in self.manifest.trees}
        actual = {tree.label: tree.digest for tree in current}
        changed = sorted(label for label in expected if actual.get(label) != expected[label])
        if changed:
            return OracleOutcome.incomplete(
                "PROTECTED_TREE_CHANGED",
                "One or more protected TD oracle trees changed after manifest creation.",
                changed=changed,
                manifest_digest=self.manifest.digest,
            )
        return OracleOutcome.ready(
            "PROTECTED_TREES_VERIFIED",
            "All protected TD oracle tree hashes match the manifest.",
            manifest_digest=self.manifest.digest,
        )


@dataclass(frozen=True)
class OracleBuildResult:
    outcome: OracleOutcome
    oracle: Optional[TDOracle] = None


@dataclass(frozen=True)
class FileApplication:
    path: str
    method: str


@dataclass(frozen=True)
class ForcingApplicationResult:
    outcome: OracleOutcome
    output_tree: Optional[Path] = None
    context: Optional[str] = None
    files: tuple[FileApplication, ...] = ()


class TestRunOutcome(str, Enum):
    PASSED = "PASSED"
    TEST_FAILURE = "TEST_FAILURE"
    INFRA = "INFRA"


@dataclass(frozen=True)
class CommandSpec:
    """Opaque command information passed to an injected calibration runner."""

    argv: tuple[str, ...]
    timeout_seconds: Optional[int] = None
    env: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class RunObservation:
    outcome: TestRunOutcome
    returncode: Optional[int]
    tests: int
    failures: int
    errors: int
    skipped: int
    build_success: bool
    detail: str = ""

    @classmethod
    def passed(cls, tests: int = 1, detail: str = "") -> "RunObservation":
        return cls(TestRunOutcome.PASSED, 0, tests, 0, 0, 0, True, detail)

    @classmethod
    def test_failure(
        cls, tests: int = 1, failures: int = 1, errors: int = 0, detail: str = ""
    ) -> "RunObservation":
        return cls(
            TestRunOutcome.TEST_FAILURE, 1, tests, failures, errors, 0, False, detail
        )

    @classmethod
    def infra(cls, detail: str, returncode: Optional[int] = None) -> "RunObservation":
        return cls(TestRunOutcome.INFRA, returncode, 0, 0, 0, 0, False, detail)


class CalibrationRunner(Protocol):
    def __call__(self, tree: Path, command: CommandSpec) -> RunObservation:
        ...


@dataclass(frozen=True)
class CalibrationRecord:
    tree: str
    run: int
    # ``None`` means either sound executed outcome is accepted.  FP is an
    # adversarial reference stress tree in some TD fixtures, not a guaranteed
    # passing ground-truth control (APEXCORE-617 demonstrably fails there).
    expected: Optional[TestRunOutcome]
    observed: RunObservation


@dataclass(frozen=True)
class CalibrationResult:
    outcome: OracleOutcome
    records: tuple[CalibrationRecord, ...]


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return _sha256(raw)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _walk_files(root: Path) -> Iterable[tuple[str, Path]]:
    """Yield stable, non-build file paths without following directory links."""

    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_parts = current_path.relative_to(root).parts
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORED_DIRS
            and not (d == "build" and "src" not in relative_parts)
        )
        for name in sorted(files):
            if name in IGNORED_FILES or name.startswith("._"):
                continue
            path = current_path / name
            yield path.relative_to(root).as_posix(), path


def _source_path(relative: str) -> bool:
    path = Path(relative)
    if path.name in SOURCE_FILENAMES:
        return True
    return path.suffix.lower() in SOURCE_SUFFIXES and (
        "src" in path.parts or path.suffix.lower() in {".java", ".kt", ".groovy", ".scala"}
    )


def _file_bytes(path: Path) -> bytes:
    if path.is_symlink():
        return b"SYMLINK\0" + os.readlink(path).encode("utf-8", "surrogateescape")
    return path.read_bytes()


def build_tree_manifest(root: Path, label: str) -> TreeManifest:
    root = Path(root)
    records: list[FileRecord] = []
    for relative, path in _walk_files(root):
        data = _file_bytes(path)
        kind = "symlink" if path.is_symlink() else "file"
        records.append(FileRecord(relative, _sha256(data), len(data), kind))
    payload = {"label": label, "files": [record.to_dict() for record in records]}
    return TreeManifest(label, _canonical_digest(payload), tuple(records))


def _text_lines(data: bytes) -> Optional[list[str]]:
    if b"\0" in data:
        return None
    try:
        return data.decode("utf-8", "surrogateescape").splitlines(keepends=True)
    except UnicodeError:
        return None


def _derive_hunks(base: bytes, forced: bytes, context_lines: int = 3) -> tuple[TextHunk, ...]:
    base_lines = _text_lines(base)
    forced_lines = _text_lines(forced)
    if base_lines is None or forced_lines is None:
        return ()
    matcher = difflib.SequenceMatcher(None, base_lines, forced_lines, autojunk=False)
    hunks: list[TextHunk] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        hunks.append(TextHunk(
            old=tuple(base_lines[i1:i2]),
            new=tuple(forced_lines[j1:j2]),
            before=tuple(base_lines[max(0, i1 - context_lines):i1]),
            after=tuple(base_lines[i2:i2 + context_lines]),
        ))
    return tuple(hunks)


def derive_source_forcing(context: str, base_root: Path, forced_root: Path) -> ForcingDelta:
    """Derive a build-artifact-free, textual forcing delta between two trees."""

    base_root, forced_root = Path(base_root), Path(forced_root)
    base_files = {rel: path for rel, path in _walk_files(base_root) if _source_path(rel)}
    forced_files = {
        rel: path for rel, path in _walk_files(forced_root) if _source_path(rel)
    }
    deltas: list[FileDelta] = []
    for relative in sorted(set(base_files) | set(forced_files)):
        base_path, forced_path = base_files.get(relative), forced_files.get(relative)
        base_data = _file_bytes(base_path) if base_path else None
        forced_data = _file_bytes(forced_path) if forced_path else None
        if base_data == forced_data:
            continue
        if base_data is None:
            kind = ChangeKind.ADD
            hunks: tuple[TextHunk, ...] = ()
        elif forced_data is None:
            kind = ChangeKind.DELETE
            hunks = ()
        else:
            kind = ChangeKind.MODIFY
            hunks = _derive_hunks(base_data, forced_data)
        deltas.append(FileDelta(
            path=relative,
            kind=kind,
            base_sha256=_sha256(base_data) if base_data is not None else None,
            forced_sha256=_sha256(forced_data) if forced_data is not None else None,
            hunks=hunks,
        ))
    payload = {"context": context, "files": [delta.to_dict() for delta in deltas]}
    return ForcingDelta(
        context=context,
        base_root=base_root,
        forced_root=forced_root,
        files=tuple(deltas),
        digest=_canonical_digest(payload),
    )


def build_oracle(trees: ProtectedTrees) -> OracleBuildResult:
    """Validate and hash B/P/F/FP, then derive both forcing contexts."""

    labelled = trees.labelled()
    missing = [label for label, path in labelled if not Path(path).is_dir()]
    if missing:
        return OracleBuildResult(OracleOutcome.incomplete(
            "MISSING_PROTECTED_TREE",
            "The semantic TD oracle requires all four protected trees.",
            missing=missing,
        ))
    resolved = [Path(path).resolve() for _label, path in labelled]
    if len(set(resolved)) != 4:
        return OracleBuildResult(OracleOutcome.incomplete(
            "PROTECTED_TREES_NOT_DISTINCT",
            "B/P/F/FP must be four distinct directories.",
        ))

    try:
        pristine = derive_source_forcing("pristine", trees.pristine, trees.perturbed)
        reference = derive_source_forcing("reference", trees.fixed, trees.fixed_perturbed)
    except OSError as exc:
        return OracleBuildResult(OracleOutcome.incomplete(
            "ORACLE_DERIVATION_ERROR",
            "The protected source trees could not be compared.",
            error=str(exc),
        ))
    if not pristine.files:
        return OracleBuildResult(OracleOutcome.incomplete(
            "EMPTY_PRISTINE_FORCING",
            "B -> P changes no supported source file.",
        ))
    if not reference.files:
        return OracleBuildResult(OracleOutcome.incomplete(
            "EMPTY_REFERENCE_FORCING",
            "F -> FP changes no supported source file; anchor-removing fixes "
            "cannot be scored soundly.",
        ))

    unsupported = [
        f"{forcing.context}:{delta.path}"
        for forcing in (pristine, reference)
        for delta in forcing.files
        if delta.kind is ChangeKind.MODIFY and not delta.hunks
    ]
    if unsupported:
        return OracleBuildResult(OracleOutcome.incomplete(
            "UNSUPPORTED_SOURCE_DELTA",
            "A forcing modifies a binary or undecodable source file.",
            files=unsupported,
        ))

    try:
        tree_manifests = tuple(
            build_tree_manifest(path, label) for label, path in labelled
        )
    except OSError as exc:
        return OracleBuildResult(OracleOutcome.incomplete(
            "ORACLE_MANIFEST_ERROR",
            "The protected source trees could not be hashed.",
            error=str(exc),
        ))
    unsigned = {
        "schema": MANIFEST_SCHEMA,
        "trees": [manifest.to_dict() for manifest in tree_manifests],
        "forcing": {"pristine": pristine.digest, "reference": reference.digest},
    }
    digest = _canonical_digest(unsigned)
    manifest = OracleManifest(
        MANIFEST_SCHEMA, tree_manifests, pristine.digest, reference.digest, digest
    )
    oracle = TDOracle(trees, pristine, reference, manifest)
    return OracleBuildResult(OracleOutcome.ready(
        "ORACLE_READY",
        "Protected B/P/F/FP manifests and dual forcing contexts are ready.",
        manifest_digest=digest,
        pristine_files=[delta.path for delta in pristine.files],
        reference_files=[delta.path for delta in reference.files],
    ), oracle)


def _find_sequence(haystack: Sequence[str], needle: Sequence[str]) -> list[int]:
    if not needle:
        return []
    width = len(needle)
    return [
        index for index in range(0, len(haystack) - width + 1)
        if list(haystack[index:index + width]) == list(needle)
    ]


def _unique_context_position(
    lines: Sequence[str], context: Sequence[str], *, before: bool
) -> Optional[int]:
    """Return a unique boundary, preferring the strongest available context."""

    max_width = min(3, len(context))
    for width in range(max_width, 0, -1):
        needle = list(context[-width:] if before else context[:width])
        matches = _find_sequence(lines, needle)
        if len(matches) == 1:
            return matches[0] + (width if before else 0)
    return None


def _apply_anchored_hunks(candidate: bytes, hunks: Sequence[TextHunk]) -> Optional[bytes]:
    """Replay exact hunks using unique anchors; never uses fuzzy line matching.

    Insertions prefer the following semantic statement.  This is important for
    reference-context TD forcing: if a candidate adds a startup barrier before
    the reference's ``activate()`` call, the delay is still inserted immediately
    before ``activate()`` rather than before the barrier.
    """

    lines = _text_lines(candidate)
    if lines is None:
        return None
    for hunk in reversed(tuple(hunks)):
        old, new = list(hunk.old), list(hunk.new)
        if old:
            matches = _find_sequence(lines, old)
            if len(matches) != 1:
                return None
            index = matches[0]
            lines[index:index + len(old)] = new
            continue

        # Pure insertion: a following reference statement defines the semantic
        # location and is therefore mandatory. Falling back to an earlier brace
        # after that statement vanished can move a delay to a different event
        # and silently turn an overlap into a bogus PASSABLE tree. Only an
        # insertion at end-of-file (no following context by construction) may
        # use the preceding context.
        if hunk.after:
            index = _unique_context_position(lines, hunk.after, before=False)
        else:
            index = _unique_context_position(lines, hunk.before, before=True)
        if index is None:
            return None
        if new and list(lines[index:index + len(new)]) == new:
            continue
        lines[index:index] = new
    return "".join(lines).encode("utf-8", "surrogateescape")


def _forcing_effect_present(data: bytes, delta: FileDelta) -> bool:
    lines = _text_lines(data)
    if lines is None:
        return False
    for hunk in delta.hunks:
        if hunk.new and not _find_sequence(lines, hunk.new):
            return False
        if not hunk.new and hunk.old and _find_sequence(lines, hunk.old):
            return False
    return True


def _merge_file(base: Path, forced: Path, candidate: Path) -> tuple[Optional[bytes], str]:
    """Run a read-only per-file 3-way merge and classify its result."""

    try:
        proc = subprocess.run(
            [
                "git", "merge-file", "-p",
                "-L", "forcing", "-L", "reference", "-L", "candidate",
                str(forced), str(base), str(candidate),
            ],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        return None, f"git merge-file unavailable: {exc}"
    if proc.returncode == 0:
        return proc.stdout, "git-merge-file"
    if 0 < proc.returncode < 128:
        return None, f"overlap ({proc.returncode} conflict hunk(s))"
    detail = (proc.stderr or b"").decode("utf-8", "replace").strip()
    return None, f"git merge-file error rc={proc.returncode}: {detail[:300]}"


def _copy_ignore_for(root: Path):
    root = Path(root).resolve()

    def _ignore(directory: str, names: list[str]) -> set[str]:
        try:
            relative_parts = Path(directory).resolve().relative_to(root).parts
        except (OSError, ValueError):
            relative_parts = ()
        return {
            name for name in names
            if name in IGNORED_DIRS
            or (name == "build" and "src" not in relative_parts)
            or name in IGNORED_FILES
            or name.startswith("._")
        }

    return _ignore


def _escaping_symlinks(root: Path) -> list[str]:
    """Return candidate links that would escape after copying to an evaluator."""

    root_resolved = root.resolve()
    escaping: list[str] = []
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        relative_parts = current_path.relative_to(root).parts
        dirs[:] = sorted(
            d for d in dirs
            if d not in IGNORED_DIRS
            and not (d == "build" and "src" not in relative_parts)
        )
        for name in tuple(dirs) + tuple(files):
            path = current_path / name
            if not path.is_symlink():
                continue
            target = Path(os.readlink(path))
            if target.is_absolute():
                escaping.append(path.relative_to(root).as_posix())
                continue
            resolved_target = (path.parent / target).resolve(strict=False)
            if not _is_relative_to(resolved_target, root_resolved):
                escaping.append(path.relative_to(root).as_posix())
    return sorted(escaping)


def _safe_destination(candidate: Path, destination: Path, trees: ProtectedTrees) -> OracleOutcome:
    if not candidate.is_dir():
        return OracleOutcome.incomplete(
            "MISSING_CANDIDATE_TREE", "The candidate tree does not exist.", path=str(candidate)
        )
    try:
        candidate_resolved = candidate.resolve()
        destination_resolved = destination.resolve(strict=False)
        protected = [path.resolve() for _label, path in trees.labelled()]
    except OSError as exc:
        return OracleOutcome.incomplete(
            "TREE_RESOLUTION_ERROR",
            "Candidate, output, or protected paths could not be resolved safely.",
            error=str(exc),
        )
    if destination.exists():
        return OracleOutcome.incomplete(
            "DESTINATION_EXISTS",
            "The forced candidate destination must not already exist.",
            path=str(destination),
        )
    try:
        escaping = _escaping_symlinks(candidate)
    except OSError as exc:
        return OracleOutcome.incomplete(
            "CANDIDATE_TREE_READ_ERROR",
            "The candidate tree could not be checked for path escapes.",
            error=str(exc),
        )
    if escaping:
        return OracleOutcome.incomplete(
            "CANDIDATE_SYMLINK_ESCAPE",
            "The candidate contains symlinks that escape its isolated tree.",
            paths=escaping,
        )
    if (
        destination_resolved == candidate_resolved
        or _is_relative_to(destination_resolved, candidate_resolved)
        or _is_relative_to(candidate_resolved, destination_resolved)
        or any(
            destination_resolved == root
            or _is_relative_to(destination_resolved, root)
            or _is_relative_to(root, destination_resolved)
            for root in protected
        )
    ):
        return OracleOutcome.incomplete(
            "UNSAFE_DESTINATION",
            "The output must be disjoint from the candidate and all protected trees.",
            path=str(destination),
        )
    return OracleOutcome.ready("SAFE_DESTINATION", "The output destination is isolated.")


def apply_reference_forcing(
    oracle: TDOracle,
    candidate_tree: Path,
    destination: Path,
    *,
    allow_pristine_fallback: bool = False,
    forcing_context: str = "reference",
) -> ForcingApplicationResult:
    """Materialize candidate + forcing without modifying candidate or B/P/F/FP.

    The reference-fixed context is mandatory by default. Falling back after a
    reference conflict would reintroduce the exact APEX failure mode where a
    candidate compensates for the visible B->P delay but leaves the real defect.
    Integration code may request the independently calibrated pristine context
    as an additional check, or opt into a pristine fallback for legacy cases;
    the chosen context is always explicit in the result.
    """

    integrity = oracle.verify_protected_trees()
    if not integrity.passable:
        return ForcingApplicationResult(integrity)
    candidate_tree, destination = Path(candidate_tree), Path(destination)
    safe = _safe_destination(candidate_tree, destination, oracle.trees)
    if not safe.passable:
        return ForcingApplicationResult(safe)

    if forcing_context not in {"reference", "pristine"}:
        return ForcingApplicationResult(OracleOutcome.incomplete(
            "INVALID_FORCING_CONTEXT",
            "The requested forcing context is neither reference nor pristine.",
            context=forcing_context,
        ))
    attempts = [
        oracle.reference_forcing
        if forcing_context == "reference" else oracle.pristine_forcing
    ]
    if forcing_context == "reference" and allow_pristine_fallback:
        attempts.append(oracle.pristine_forcing)
    failures: list[dict[str, Any]] = []
    for forcing in attempts:
        try:
            shutil.copytree(
                candidate_tree, destination, symlinks=True,
                ignore=_copy_ignore_for(candidate_tree)
            )
        except OSError as exc:
            return ForcingApplicationResult(OracleOutcome.incomplete(
                "CANDIDATE_COPY_ERROR",
                "Could not create an isolated candidate copy.",
                error=str(exc),
            ))

        applied: list[FileApplication] = []
        failure: Optional[OracleOutcome] = None
        for delta in forcing.files:
            base = forcing.base_root / delta.path
            forced = forcing.forced_root / delta.path
            target = destination / delta.path
            try:
                output_root = destination.resolve()
                target_parent = target.parent.resolve(strict=False)
                if not _is_relative_to(target_parent, output_root):
                    failure = OracleOutcome.incomplete(
                        "CANDIDATE_PATH_ESCAPE",
                        "A candidate path would escape the isolated output tree.",
                        context=forcing.context, path=delta.path,
                    )
                    break
                if delta.kind is ChangeKind.ADD:
                    if target.exists() or target.is_symlink():
                        failure = OracleOutcome.incomplete(
                            "FORCING_ADD_OVERLAP",
                            "The forcing adds a path already created by the candidate.",
                            context=forcing.context, path=delta.path,
                        )
                        break
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(forced, target)
                    applied.append(FileApplication(delta.path, "add"))
                    continue

                if delta.kind is ChangeKind.DELETE:
                    if not target.is_file():
                        failure = OracleOutcome.incomplete(
                            "FORCING_DELETE_OVERLAP",
                            "The forcing deletes a path also removed by the candidate.",
                            context=forcing.context, path=delta.path,
                        )
                        break
                    if _sha256(target.read_bytes()) != delta.base_sha256:
                        failure = OracleOutcome.incomplete(
                            "FORCING_DELETE_OVERLAP",
                            "The candidate modified a path the forcing deletes.",
                            context=forcing.context, path=delta.path,
                        )
                        break
                    target.unlink()
                    applied.append(FileApplication(delta.path, "delete"))
                    continue

                if not target.is_file() or target.is_symlink():
                    failure = OracleOutcome.incomplete(
                        "FORCING_TARGET_MISSING",
                        "The candidate removed or replaced a source file required by the forcing.",
                        context=forcing.context, path=delta.path,
                    )
                    break

                candidate_data = target.read_bytes()
                if _sha256(candidate_data) == delta.base_sha256:
                    merged, method = forced.read_bytes(), "exact-base"
                else:
                    merged = _apply_anchored_hunks(candidate_data, delta.hunks)
                    method = "anchored-patch"
                    if merged is None:
                        merged, method = _merge_file(base, forced, target)
                if merged is None or not _forcing_effect_present(merged, delta):
                    failure = OracleOutcome.incomplete(
                        "REFERENCE_FORCING_CONFLICT",
                        "The forcing could not be replayed soundly in this "
                        "candidate; no model verdict was assigned.",
                        context=forcing.context,
                        path=delta.path,
                        merge=method,
                    )
                    break
                target.write_bytes(merged)
                applied.append(FileApplication(delta.path, method))
            except OSError as exc:
                failure = OracleOutcome.incomplete(
                    "FORCING_IO_ERROR",
                    "An I/O error prevented semantic forcing preparation.",
                    context=forcing.context, path=delta.path, error=str(exc),
                )
                break

        if failure is None:
            post_integrity = oracle.verify_protected_trees()
            if not post_integrity.passable:
                shutil.rmtree(destination, ignore_errors=True)
                return ForcingApplicationResult(post_integrity)
            return ForcingApplicationResult(
                OracleOutcome.ready(
                    "REFERENCE_FORCING_APPLIED" if forcing.context == "reference"
                    else "PRISTINE_FORCING_APPLIED",
                    "A protected forcing context was applied to an isolated candidate copy.",
                    context=forcing.context,
                    manifest_digest=oracle.manifest.digest,
                    files=[item.path for item in applied],
                ),
                destination,
                forcing.context,
                tuple(applied),
            )

        failures.append({
            "context": forcing.context,
            "code": failure.code,
            "message": failure.message,
            "details": dict(failure.details),
        })
        shutil.rmtree(destination, ignore_errors=True)

    unavailable_code = (
        "REFERENCE_FORCING_UNAVAILABLE"
        if forcing_context == "reference" else "PRISTINE_FORCING_UNAVAILABLE")
    return ForcingApplicationResult(OracleOutcome.incomplete(
        unavailable_code,
        "No protected forcing context could be applied soundly; this run is "
        "incomplete, not a model failure.",
        attempts=failures,
        manifest_digest=oracle.manifest.digest,
    ))


def _observation_is_sound(observation: RunObservation) -> bool:
    if observation.outcome is TestRunOutcome.PASSED:
        return (
            observation.returncode == 0
            and observation.tests > 0
            and observation.failures == 0
            and observation.errors == 0
            and observation.skipped == 0
            and observation.build_success
        )
    if observation.outcome is TestRunOutcome.TEST_FAILURE:
        return observation.tests > 0 and (observation.failures + observation.errors) > 0
    return True


def calibrate_oracle(
    oracle: TDOracle,
    command: CommandSpec,
    runner: CalibrationRunner,
    *,
    repetitions: int = 1,
) -> CalibrationResult:
    """Calibrate B=pass, P=fail, F=pass and require a sound FP execution.

    ``FixedCodeChange`` is useful for deriving a hidden, reference-anchored
    timing stress, but it is not universally expected to pass.  In particular,
    APEXCORE-617's supplied FP delays activation until after the reference fix
    calls shutdown, so FP genuinely fails.  Treating that as infrastructure
    would make every candidate unscorable.  We therefore record FP's observed
    outcome and accept either a strict pass or an executed test failure; zero
    tests, timeout, malformed evidence, or build failure still fail closed.
    """

    if repetitions < 1:
        return CalibrationResult(OracleOutcome.incomplete(
            "INVALID_CALIBRATION_REPETITIONS",
            "Calibration repetitions must be at least one.",
            repetitions=repetitions,
        ), ())
    integrity = oracle.verify_protected_trees()
    if not integrity.passable:
        return CalibrationResult(integrity, ())

    expected = {
        "pristine": TestRunOutcome.PASSED,
        "perturbed": TestRunOutcome.TEST_FAILURE,
        "fixed": TestRunOutcome.PASSED,
        "fixed_perturbed": None,
    }
    records: list[CalibrationRecord] = []
    for label, tree in oracle.trees.labelled():
        for run_number in range(1, repetitions + 1):
            try:
                observed = runner(tree, command)
            except Exception as exc:  # runner boundary: classify, never misscore
                observed = RunObservation.infra(f"runner raised {type(exc).__name__}: {exc}")
            record = CalibrationRecord(label, run_number, expected[label], observed)
            records.append(record)
            run_integrity = oracle.verify_protected_trees()
            if not run_integrity.passable:
                return CalibrationResult(run_integrity, tuple(records))
            if observed.outcome is TestRunOutcome.INFRA or not _observation_is_sound(observed):
                return CalibrationResult(OracleOutcome.incomplete(
                    "CALIBRATION_INFRA",
                    "A calibration command did not produce a trustworthy test result.",
                    tree=label,
                    run=run_number,
                    observed=observed.outcome.value,
                    detail=observed.detail,
                ), tuple(records))
            if (expected[label] is not None
                    and observed.outcome is not expected[label]):
                return CalibrationResult(OracleOutcome.incomplete(
                    "CALIBRATION_MISMATCH",
                    "The TD four-tree control matrix is not discriminative.",
                    tree=label,
                    run=run_number,
                    expected=expected[label].value,
                    observed=observed.outcome.value,
                ), tuple(records))

    post_integrity = oracle.verify_protected_trees()
    if not post_integrity.passable:
        return CalibrationResult(post_integrity, tuple(records))
    return CalibrationResult(OracleOutcome.ready(
        "CALIBRATED",
        "B passed, P failed, F passed, and FP produced a sound executed "
        "result for every calibration run.",
        repetitions=repetitions,
        fixed_perturbed_outcomes=[
            record.observed.outcome.value for record in records
            if record.tree == "fixed_perturbed"
        ],
        manifest_digest=oracle.manifest.digest,
    ), tuple(records))


__all__ = [
    "CalibrationRecord",
    "CalibrationResult",
    "CalibrationRunner",
    "ChangeKind",
    "CommandSpec",
    "FileApplication",
    "FileDelta",
    "FileRecord",
    "ForcingApplicationResult",
    "ForcingDelta",
    "MANIFEST_SCHEMA",
    "OracleBuildResult",
    "OracleDisposition",
    "OracleManifest",
    "OracleOutcome",
    "ProtectedTrees",
    "RunObservation",
    "TDOracle",
    "TestRunOutcome",
    "TreeManifest",
    "apply_reference_forcing",
    "build_oracle",
    "build_tree_manifest",
    "calibrate_oracle",
    "derive_source_forcing",
]
