"""Deterministic code/diff analysis tools for the Sentinel Code Agent.

These functions perform mechanical extraction only. They never call an LLM.

Incident-bundle contract observed in this repository:
- Python service code lives under service/ (often a single app.py)
- Unified diffs live at git_diff.patch and may name files that do not exist on disk
- Optional docker-compose.yml configuration
- SQL migrations may appear only inside the patch (DROP INDEX / CREATE INDEX)

References:
- Real files: service/app.py:40-42 (1-indexed, inclusive)
- Patch hunks when the named file is absent: git_diff.patch:hunk 1
- Patch file lines: git_diff.patch:14
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union


DIFF_PATH = "git_diff.patch"
SERVICE_DIR = "service"
SKIP_DIR_NAMES = {"__pycache__", "tests", ".pytest_cache"}
SKIP_FILE_NAMES = {"ground_truth.md", "ground_truth.json"}
MAX_CANDIDATES = 24

HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)
GIT_DIFF_FILE_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")
QUERY_ATTR_RE = re.compile(r"(query|execute|fetch|cursor)", re.IGNORECASE)
RETRY_CONST_RE = re.compile(r"MAX_RETRIES\s*=\s*(\d+)")
BACKOFF_ZERO_RE = re.compile(r"BACKOFF[A-Z_]*\s*=\s*0(?:\.0+)?\b")
TTL_SHORT_RE = re.compile(r"(TTL|ttl)[A-Z_]*\s*=\s*(\d+)")
TIMEOUT_RE = re.compile(r"TIMEOUT[A-Z_]*\s*=\s*(\d+(?:\.\d+)?)")
DROP_INDEX_RE = re.compile(r"\bDROP\s+INDEX\b", re.IGNORECASE)
CREATE_INDEX_RE = re.compile(r"\bCREATE\s+INDEX\b", re.IGNORECASE)
HTTP_CALL_RE = re.compile(r"\.(get|post|put|patch|delete|request)\s*\(", re.IGNORECASE)


@dataclass
class DiffHunk:
    hunk_index: int
    old_path: str
    new_path: str
    old_start: int
    new_start: int
    header: str
    patch_start_line: int
    body_lines: List[str] = field(default_factory=list)
    added: List[Tuple[int, int, str]] = field(default_factory=list)
    # (new_file_line, patch_file_line, text)
    removed: List[Tuple[int, int, str]] = field(default_factory=list)
    # (old_file_line, patch_file_line, text)


@dataclass
class GitDiff:
    text: str
    hunks: List[DiffHunk]
    changed_files: List[str]


@dataclass
class SourceHit:
    relative_path: str
    line_number: int
    text: str


@dataclass
class CodeFinding:
    source: str
    reference: str
    finding_type: str
    excerpt: str
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_candidate(self) -> Dict[str, Any]:
        return {
            "source": self.source,
            "reference": self.reference,
            "type": self.finding_type,
            "excerpt": self.excerpt,
            "metadata": self.metadata,
        }


def load_git_diff(source: Union[str, Path]) -> GitDiff:
    """Load a unified git diff from a file path or raw text."""
    path = Path(source)
    if path.exists() and path.is_file():
        text = path.read_text(encoding="utf-8", errors="ignore")
    else:
        text = str(source)
    return parse_git_diff(text)


def parse_git_diff(text: str) -> GitDiff:
    """Parse unified diff text into hunks with added/removed lines."""
    lines = text.splitlines()
    hunks: List[DiffHunk] = []
    changed: List[str] = []
    current_old = ""
    current_new = ""
    current: Optional[DiffHunk] = None
    old_line = 0
    new_line = 0

    def close() -> None:
        nonlocal current
        if current is not None:
            hunks.append(current)
            current = None

    for idx, raw in enumerate(lines, start=1):
        file_match = GIT_DIFF_FILE_RE.match(raw)
        if file_match:
            close()
            current_old = file_match.group(1)
            current_new = file_match.group(2)
            for name in (current_old, current_new):
                if name not in changed and name != "/dev/null":
                    changed.append(name)
            continue
        if raw.startswith("--- "):
            continue
        if raw.startswith("+++ "):
            continue
        hunk_match = HUNK_HEADER_RE.match(raw)
        if hunk_match:
            close()
            old_line = int(hunk_match.group(1))
            new_line = int(hunk_match.group(3))
            current = DiffHunk(
                hunk_index=len(hunks) + 1,
                old_path=current_old,
                new_path=current_new,
                old_start=old_line,
                new_start=new_line,
                header=raw,
                patch_start_line=idx,
            )
            continue
        if current is None:
            continue
        current.body_lines.append(raw)
        if raw.startswith("+") and not raw.startswith("+++"):
            current.added.append((new_line, idx, raw[1:]))
            new_line += 1
        elif raw.startswith("-") and not raw.startswith("---"):
            current.removed.append((old_line, idx, raw[1:]))
            old_line += 1
        elif raw.startswith("\\"):
            continue
        else:
            old_line += 1
            new_line += 1
    close()
    return GitDiff(text=text, hunks=hunks, changed_files=changed)


def list_changed_files(diff: GitDiff) -> List[str]:
    return list(diff.changed_files)


def extract_added_lines(diff: GitDiff) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for hunk in diff.hunks:
        for new_line, patch_line, text in hunk.added:
            items.append(
                {
                    "file": hunk.new_path,
                    "hunk_index": hunk.hunk_index,
                    "new_line": new_line,
                    "patch_line": patch_line,
                    "text": text,
                }
            )
    return items


def extract_removed_lines(diff: GitDiff) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for hunk in diff.hunks:
        for old_line, patch_line, text in hunk.removed:
            items.append(
                {
                    "file": hunk.old_path,
                    "hunk_index": hunk.hunk_index,
                    "old_line": old_line,
                    "patch_line": patch_line,
                    "text": text,
                }
            )
    return items


def extract_hunks(diff: GitDiff) -> List[DiffHunk]:
    return list(diff.hunks)


def iter_source_files(incident_dir: Union[str, Path]) -> List[Path]:
    """Yield service Python files, excluding tests and evaluation files."""
    root = Path(incident_dir)
    service = root / SERVICE_DIR
    if not service.exists():
        return []
    files: List[Path] = []
    for path in sorted(service.rglob("*.py")):
        if any(part in SKIP_DIR_NAMES for part in path.parts):
            continue
        if path.name in SKIP_FILE_NAMES:
            continue
        files.append(path)
    return files


def search_source(
    incident_dir: Union[str, Path],
    pattern: str,
    ignore_case: bool = False,
) -> List[SourceHit]:
    """Search service source files for a string or regex."""
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        compiled = re.compile(re.escape(pattern), flags)
    root = Path(incident_dir)
    hits: List[SourceHit] = []
    for path in iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            if compiled.search(line):
                hits.append(SourceHit(relative_path=rel, line_number=line_no, text=line))
    return hits


def get_source_context(
    incident_dir: Union[str, Path],
    relative_path: str,
    line_number: int,
    before: int = 2,
    after: int = 2,
) -> List[SourceHit]:
    """Return nearby original lines around a source line number."""
    path = Path(incident_dir) / relative_path
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    start = max(1, line_number - max(0, before))
    end = min(len(lines), line_number + max(0, after))
    hits: List[SourceHit] = []
    for n in range(start, end + 1):
        hits.append(SourceHit(relative_path=relative_path, line_number=n, text=lines[n - 1]))
    return hits


def _source_range_ref(rel: str, start: int, end: int) -> str:
    if start == end:
        return f"{rel}:{start}"
    return f"{rel}:{start}-{end}"


def _locate_snippet_in_source(incident_dir: Path, snippet: str) -> Optional[SourceHit]:
    needle = snippet.strip()
    if len(needle) < 8:
        return None
    for path in iter_source_files(incident_dir):
        rel = path.relative_to(incident_dir).as_posix()
        for line_no, line in enumerate(path.read_text(encoding="utf-8", errors="ignore").splitlines(), start=1):
            if needle in line or line.strip() == needle:
                return SourceHit(relative_path=rel, line_number=line_no, text=line)
    return None


def _hunk_reference(hunk: DiffHunk) -> str:
    return f"{DIFF_PATH}:hunk {hunk.hunk_index}"


def detect_suspicious_patterns(
    incident_dir: Union[str, Path],
    diff: Optional[GitDiff] = None,
) -> List[CodeFinding]:
    """Detect generic potentially risky patterns. Reports locations, not root causes."""
    root = Path(incident_dir)
    findings: List[CodeFinding] = []
    findings.extend(_detect_in_source(root))
    if diff is not None:
        findings.extend(_detect_in_diff(root, diff))
    return _dedupe_findings(findings)


def _detect_in_source(root: Path) -> List[CodeFinding]:
    findings: List[CodeFinding] = []
    for path in iter_source_files(root):
        rel = path.relative_to(root).as_posix()
        text = path.read_text(encoding="utf-8", errors="ignore")
        lines = text.splitlines()
        findings.extend(_ast_patterns(rel, text, lines))
        findings.extend(_line_patterns(rel, "code", lines, source_kind="code"))
    compose = root / "docker-compose.yml"
    if compose.exists():
        clines = compose.read_text(encoding="utf-8", errors="ignore").splitlines()
        findings.extend(_line_patterns("docker-compose.yml", "config", clines, source_kind="config"))
    return findings


def _ast_patterns(rel: str, text: str, lines: Sequence[str]) -> List[CodeFinding]:
    findings: List[CodeFinding] = []
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return findings

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.While)) and getattr(node, "lineno", None):
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                name = _call_name(child)
                if name and QUERY_ATTR_RE.search(name):
                    start = node.lineno
                    end = getattr(node, "end_lineno", start) or start
                    excerpt = "\n".join(lines[start - 1 : end])
                    findings.append(
                        CodeFinding(
                            source="code",
                            reference=_source_range_ref(rel, start, end),
                            finding_type="suspicious_pattern",
                            excerpt=excerpt,
                            metadata={"pattern": "query_or_db_call_inside_loop"},
                        )
                    )
                    break
            if isinstance(node, ast.While):
                excerpt = "\n".join(lines[node.lineno - 1 : (getattr(node, "end_lineno", node.lineno) or node.lineno)])
                if "retry" in excerpt.lower() or "MAX_RETRIES" in text:
                    findings.append(
                        CodeFinding(
                            source="code",
                            reference=_source_range_ref(
                                rel, node.lineno, getattr(node, "end_lineno", node.lineno) or node.lineno
                            ),
                            finding_type="suspicious_pattern",
                            excerpt=excerpt,
                            metadata={"pattern": "retry_loop"},
                        )
                    )

        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                target_name = None
                if isinstance(stmt, ast.Assign) and stmt.targets and isinstance(stmt.targets[0], ast.Name):
                    if isinstance(stmt.value, (ast.Dict, ast.List, ast.Set)):
                        target_name = stmt.targets[0].id
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    if stmt.value is not None and isinstance(stmt.value, (ast.Dict, ast.List, ast.Set)):
                        target_name = stmt.target.id
                    elif stmt.annotation is not None:
                        ann = ast.unparse(stmt.annotation) if hasattr(ast, "unparse") else ""
                        if "Dict" in ann or "List" in ann or "Set" in ann:
                            target_name = stmt.target.id
                if target_name:
                    lineno = stmt.lineno
                    findings.append(
                        CodeFinding(
                            source="code",
                            reference=f"{rel}:{lineno}",
                            finding_type="suspicious_pattern",
                            excerpt=lines[lineno - 1],
                            metadata={"pattern": "class_level_mutable_collection", "name": target_name},
                        )
                    )

        if isinstance(node, ast.FunctionDef):
            findings.extend(_acquire_without_release(rel, node, lines))
    return findings


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _acquire_without_release(rel: str, node: ast.FunctionDef, lines: Sequence[str]) -> List[CodeFinding]:
    src = ast.get_source_segment("\n".join(lines), node) or "\n".join(
        lines[node.lineno - 1 : (getattr(node, "end_lineno", node.lineno) or node.lineno)]
    )
    if ".acquire(" not in src and "acquire(" not in src:
        return []
    has_release = ".release(" in src or "release(" in src
    has_finally = "finally:" in src
    has_with = re.search(r"\bwith\b", src) is not None
    if has_release and has_finally:
        return []
    if has_with:
        return []
    if has_release and "raise " not in src:
        return []
    # acquire present; release missing, or raise may skip release
    if (not has_release) or ("raise " in src and not has_finally):
        start = node.lineno
        end = getattr(node, "end_lineno", start) or start
        return [
            CodeFinding(
                source="code",
                reference=_source_range_ref(rel, start, end),
                finding_type="suspicious_pattern",
                excerpt="\n".join(lines[start - 1 : end]),
                metadata={"pattern": "connection_acquire_without_guaranteed_release"},
            )
        ]
    return []


def _line_patterns(
    rel: str,
    source: str,
    lines: Sequence[str],
    source_kind: str,
) -> List[CodeFinding]:
    findings: List[CodeFinding] = []
    for i, line in enumerate(lines, start=1):
        stripped = line.strip()
        if DROP_INDEX_RE.search(line):
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "drop_index"},
                )
            )
        retry = RETRY_CONST_RE.search(line)
        if retry and int(retry.group(1)) >= 5:
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "high_retry_count", "value": int(retry.group(1))},
                )
            )
        if BACKOFF_ZERO_RE.search(line):
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "zero_backoff"},
                )
            )
        ttl = TTL_SHORT_RE.search(line)
        if ttl and int(ttl.group(2)) <= 30:
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "short_cache_ttl", "value": int(ttl.group(2))},
                )
            )
        timeout = TIMEOUT_RE.search(line)
        if timeout and float(timeout.group(1)) >= 30:
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "long_timeout", "value": float(timeout.group(1))},
                )
            )
        if "circuit breaker disabled" in line.lower() or "CIRCUIT_BREAKER" in line and "False" in line:
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "circuit_breaker_disabled"},
                )
            )
        if HTTP_CALL_RE.search(line) and "timeout" not in line.lower():
            # still record blocking HTTP; timeout may be in kwargs on same line
            if "http" in line.lower() or "client" in line.lower():
                findings.append(
                    CodeFinding(
                        source=source,
                        reference=f"{rel}:{i}",
                        finding_type="suspicious_pattern",
                        excerpt=line,
                        metadata={"pattern": "outbound_http_call"},
                    )
                )
        if source_kind == "code" and re.search(r"get_stock\(|current\s*=", line) and i < len(lines):
            # check-then-act is better caught across a few lines in collect; skip here
            pass
        if stripped.startswith("CREATE INDEX") or CREATE_INDEX_RE.search(line):
            findings.append(
                CodeFinding(
                    source=source,
                    reference=f"{rel}:{i}",
                    finding_type="suspicious_pattern",
                    excerpt=line,
                    metadata={"pattern": "create_index"},
                )
            )
    return findings


def _detect_in_diff(root: Path, diff: GitDiff) -> List[CodeFinding]:
    findings: List[CodeFinding] = []
    for hunk in diff.hunks:
        added_text = "\n".join(text for _, _, text in hunk.added)
        removed_text = "\n".join(text for _, _, text in hunk.removed)
        combined = added_text + "\n" + removed_text
        patterns = []
        if any(QUERY_ATTR_RE.search(t) for _, _, t in hunk.added) and any(
            t.lstrip().startswith("for ") for _, _, t in hunk.added
        ):
            patterns.append("query_inside_added_loop")
        if DROP_INDEX_RE.search(combined):
            patterns.append("drop_index")
        if any(CREATE_INDEX_RE.search(t) for _, _, t in hunk.removed):
            patterns.append("index_creation_removed")
        if RETRY_CONST_RE.search(added_text):
            match = RETRY_CONST_RE.search(added_text)
            if match and int(match.group(1)) >= 5:
                patterns.append("high_retry_count")
        if BACKOFF_ZERO_RE.search(added_text):
            patterns.append("zero_backoff")
        if TTL_SHORT_RE.search(added_text):
            ttl = TTL_SHORT_RE.search(added_text)
            if ttl and int(ttl.group(2)) <= 30:
                patterns.append("short_cache_ttl")
        if TIMEOUT_RE.search(added_text):
            to = TIMEOUT_RE.search(added_text)
            if to and float(to.group(1)) >= 30:
                patterns.append("long_timeout")
        if "circuit breaker disabled" in added_text.lower():
            patterns.append("circuit_breaker_disabled")
        if HTTP_CALL_RE.search(added_text):
            patterns.append("outbound_http_call")
        if "acquire(" in added_text and "raise " in added_text and "release(" not in added_text:
            patterns.append("connection_acquire_without_guaranteed_release")
        if re.search(r"=\s*\{\s*\}", added_text) or "REGISTRY" in added_text:
            patterns.append("unbounded_collection")
        if "get_stock" in added_text and "set_stock" in added_text:
            patterns.append("check_then_act")

        if not patterns and not hunk.added and not hunk.removed:
            continue
        excerpt_lines = []
        for _, _, text in hunk.removed:
            excerpt_lines.append("-" + text)
        for _, _, text in hunk.added:
            excerpt_lines.append("+" + text)
        excerpt = "\n".join(excerpt_lines) if excerpt_lines else hunk.header
        for pattern in patterns or ["diff_hunk"]:
            if pattern == "diff_hunk":
                continue
            findings.append(
                CodeFinding(
                    source="git_diff",
                    reference=_hunk_reference(hunk),
                    finding_type="suspicious_pattern",
                    excerpt=excerpt,
                    metadata={"pattern": pattern, "diff_file": hunk.new_path},
                )
            )
    return findings


def _dedupe_findings(findings: List[CodeFinding]) -> List[CodeFinding]:
    seen: set[Tuple[str, str, str]] = set()
    unique: List[CodeFinding] = []
    for item in findings:
        key = (item.reference, item.finding_type, item.excerpt.strip())
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def collect_candidate_evidence(
    incident_dir: Union[str, Path],
    max_items: int = MAX_CANDIDATES,
) -> List[Dict[str, Any]]:
    """Build a concise code/diff evidence bundle with real references and excerpts."""
    root = Path(incident_dir)
    diff_path = root / DIFF_PATH
    diff: Optional[GitDiff] = None
    if diff_path.exists():
        diff = load_git_diff(diff_path)

    findings: List[CodeFinding] = []

    if diff is not None:
        for hunk in diff.hunks:
            if hunk.added:
                lines = [text for _, _, text in hunk.added]
                first_patch = hunk.added[0][1]
                last_patch = hunk.added[-1][1]
                hit = _locate_snippet_in_source(root, lines[0]) if lines else None
                use_source = False
                if hit:
                    end = hit.line_number + max(0, len(lines) - 1)
                    src_file = root / hit.relative_path
                    if src_file.exists():
                        src_lines = src_file.read_text(encoding="utf-8", errors="ignore").splitlines()
                        block = [l.strip() for l in src_lines[hit.line_number - 1 : end]]
                        snippet = [ln.strip() for ln in lines]
                        # verify all snippet lines appear contiguously in block; allow for
                        # unchanged diff context lines interleaved by checking subset match
                        matches = True
                        j = 0
                        for src_line in block:
                            if j >= len(snippet):
                                break
                            if src_line == snippet[j]:
                                j += 1
                        if j == len(snippet):
                            use_source = True
                if use_source:
                    ref = _source_range_ref(hit.relative_path, hit.line_number, end)
                    source = "code"
                    excerpt = "\n".join(lines)
                else:
                    ref = _hunk_reference(hunk)
                    source = "git_diff"
                    excerpt = "\n".join("+" + t for t in lines)
                findings.append(
                    CodeFinding(
                        source=source,
                        reference=ref,
                        finding_type="added_code",
                        excerpt=excerpt,
                        metadata={"hunk_index": hunk.hunk_index, "patch_lines": f"{first_patch}-{last_patch}"},
                    )
                )
            if hunk.removed:
                lines = [text for _, _, text in hunk.removed]
                findings.append(
                    CodeFinding(
                        source="git_diff",
                        reference=_hunk_reference(hunk),
                        finding_type="removed_code",
                        excerpt="\n".join("-" + t for t in lines),
                        metadata={"hunk_index": hunk.hunk_index, "diff_file": hunk.old_path},
                    )
                )

        sqlish = any(
            hunk.new_path.endswith(".sql") or hunk.old_path.endswith(".sql") for hunk in diff.hunks
        )
        if sqlish:
            for hunk in diff.hunks:
                findings.append(
                    CodeFinding(
                        source="git_diff",
                        reference=_hunk_reference(hunk),
                        finding_type="changed_config",
                        excerpt="\n".join(
                            ["-" + t for _, _, t in hunk.removed] + ["+" + t for _, _, t in hunk.added]
                        ),
                        metadata={"diff_file": hunk.new_path},
                    )
                )

    findings.extend(detect_suspicious_patterns(root, diff))

    unique = _dedupe_findings(findings)

    def rank(item: CodeFinding) -> Tuple[int, int]:
        type_rank = {
            "suspicious_pattern": 0,
            "added_code": 1,
            "removed_code": 2,
            "changed_config": 3,
        }
        return (type_rank.get(item.finding_type, 9), -len(item.excerpt))

    unique.sort(key=rank)
    return [item.to_candidate() for item in unique[:max_items]]
