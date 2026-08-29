"""Deterministic log analysis tools for the Sentinel Logs Agent.

These functions perform mechanical extraction only. They never call an LLM.
Line numbers are 1-indexed and refer to the original file contents.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Union


TIMESTAMP_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z?)\b"
)
LEVEL_RE = re.compile(
    r"\b(?P<level>INFO|WARN|WARNING|ERROR|FATAL|DEBUG|TRACE|CRITICAL)\b"
)
SERVICE_RE = re.compile(r"\[(?P<service>[a-zA-Z0-9._-]+)\]")
REQUEST_ID_RE = re.compile(
    r"(?:request[_-]?id|correlation[_-]?id|trace[_-]?id|corr[_-]?id|req[_-]?id)"
    r"\s*[=:]\s*([A-Za-z0-9._-]+)",
    re.IGNORECASE,
)
UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
CHARGE_ID_RE = re.compile(r"\b(?:charge_id|order_id|txn_id)\s*=\s*([A-Za-z0-9._-]+)", re.IGNORECASE)

ERROR_LEVELS = {"ERROR", "FATAL", "CRITICAL"}
WARNING_LEVELS = {"WARN", "WARNING"}
EXCEPTION_HINT_RE = re.compile(
    r"\b(exception|traceback|timeout(?:acquiringconnection|error)?|fatal)\b",
    re.IGNORECASE,
)

DEFAULT_RELATIVE_LOG = "logs/application.log"


@dataclass
class LogLine:
    """A single original log line with parsed metadata."""

    line_number: int
    text: str
    timestamp: Optional[str] = None
    timestamp_dt: Optional[datetime] = None
    level: Optional[str] = None
    service: Optional[str] = None


@dataclass
class LogMatch:
    """A deterministic match against original log text."""

    line_number: int
    text: str
    timestamp: Optional[str] = None
    level: Optional[str] = None
    service: Optional[str] = None
    match_type: str = "pattern"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def reference(self, relative_path: str = DEFAULT_RELATIVE_LOG) -> str:
        return f"{relative_path}:{self.line_number}"

    def to_candidate(self, relative_path: str = DEFAULT_RELATIVE_LOG) -> Dict[str, Any]:
        return {
            "source": "logs",
            "reference": self.reference(relative_path),
            "timestamp": self.timestamp or "",
            "excerpt": self.text,
            "type": self.match_type,
            "line_number": self.line_number,
            "metadata": self.metadata,
        }


def load_log_lines(source: Union[str, Path, Sequence[str]]) -> List[LogLine]:
    """Load original log lines from a file path or in-memory text/list.

    Empty trailing lines produced by a final newline are dropped. Line numbers
    remain 1-indexed against the surviving original lines.
    """
    if isinstance(source, (str, Path)) and Path(source).exists() and Path(source).is_file():
        raw_text = Path(source).read_text(encoding="utf-8", errors="ignore")
        raw_lines = raw_text.splitlines()
    elif isinstance(source, Path):
        raw_text = source.read_text(encoding="utf-8", errors="ignore")
        raw_lines = raw_text.splitlines()
    elif isinstance(source, str):
        raw_lines = source.splitlines()
    else:
        raw_lines = list(source)

    parsed: List[LogLine] = []
    for idx, text in enumerate(raw_lines, start=1):
        parsed.append(_parse_line(idx, text))
    return parsed


def _parse_line(line_number: int, text: str) -> LogLine:
    ts_match = TIMESTAMP_RE.match(text.strip())
    timestamp = ts_match.group("ts") if ts_match else None
    timestamp_dt = _parse_timestamp(timestamp) if timestamp else None

    level_match = LEVEL_RE.search(text)
    level = level_match.group("level") if level_match else None
    if level == "WARN":
        level = "WARN"

    service_match = SERVICE_RE.search(text)
    service = service_match.group("service") if service_match else None

    return LogLine(
        line_number=line_number,
        text=text,
        timestamp=timestamp,
        timestamp_dt=timestamp_dt,
        level=level,
        service=service,
    )


def _parse_timestamp(value: str) -> Optional[datetime]:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _as_match(line: LogLine, match_type: str, **metadata: Any) -> LogMatch:
    return LogMatch(
        line_number=line.line_number,
        text=line.text,
        timestamp=line.timestamp,
        level=line.level,
        service=line.service,
        match_type=match_type,
        metadata=metadata,
    )


def search_log(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    pattern: str,
    ignore_case: bool = False,
) -> List[LogMatch]:
    """Search for a string or regular expression. Returns original matching lines."""
    lines = _ensure_lines(source)
    flags = re.IGNORECASE if ignore_case else 0
    try:
        compiled = re.compile(pattern, flags)
    except re.error:
        compiled = re.compile(re.escape(pattern), flags)

    matches: List[LogMatch] = []
    for line in lines:
        found = compiled.search(line.text)
        if found:
            matches.append(
                _as_match(line, "pattern", pattern=pattern, matched=found.group(0))
            )
    return matches


def find_error_lines(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
) -> List[LogMatch]:
    """Find ERROR/FATAL/CRITICAL/exception-style lines."""
    lines = _ensure_lines(source)
    matches: List[LogMatch] = []
    for line in lines:
        level = (line.level or "").upper()
        if level in ERROR_LEVELS:
            matches.append(_as_match(line, "error"))
        elif level not in WARNING_LEVELS and EXCEPTION_HINT_RE.search(line.text):
            # Only treat exception-style text as errors when the line is not already a WARN.
            matches.append(_as_match(line, "error"))
    return matches


def find_warning_lines(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
) -> List[LogMatch]:
    """Find WARNING/WARN lines."""
    lines = _ensure_lines(source)
    matches: List[LogMatch] = []
    for line in lines:
        level = (line.level or "").upper()
        if level in WARNING_LEVELS or level == "WARN":
            matches.append(_as_match(line, "warning"))
    return matches


def count_pattern(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    pattern: str,
    ignore_case: bool = False,
) -> int:
    """Count occurrences of a pattern across the log (match-per-line)."""
    return len(search_log(source, pattern, ignore_case=ignore_case))


def find_bursts(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    window_seconds: int = 30,
    min_events: int = 3,
    levels: Optional[Iterable[str]] = None,
) -> List[LogMatch]:
    """Detect unusually dense timestamped events.

    Returns the first line of each burst plus metadata describing the cluster.
    Lines without parseable timestamps are ignored.
    """
    lines = _ensure_lines(source)
    interesting_levels = {lvl.upper() for lvl in levels} if levels else (ERROR_LEVELS | WARNING_LEVELS)

    timed: List[LogLine] = []
    for line in lines:
        if line.timestamp_dt is None:
            continue
        level = (line.level or "").upper()
        if level in interesting_levels:
            timed.append(line)

    bursts: List[LogMatch] = []
    used: set[int] = set()
    for i, start in enumerate(timed):
        if start.line_number in used:
            continue
        cluster = [start]
        for later in timed[i + 1 :]:
            delta = (later.timestamp_dt - start.timestamp_dt).total_seconds()
            if delta < 0:
                continue
            if delta <= window_seconds:
                cluster.append(later)
            else:
                break
        if len(cluster) >= min_events:
            for member in cluster:
                used.add(member.line_number)
            bursts.append(
                _as_match(
                    cluster[0],
                    "burst",
                    window_seconds=window_seconds,
                    event_count=len(cluster),
                    line_numbers=[m.line_number for m in cluster],
                    end_timestamp=cluster[-1].timestamp,
                )
            )
    return bursts


def extract_time_window(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    start: str,
    end: str,
) -> List[LogMatch]:
    """Return log entries whose timestamps fall between start and end inclusive."""
    lines = _ensure_lines(source)
    start_dt = _parse_timestamp(start)
    end_dt = _parse_timestamp(end)
    if start_dt is None or end_dt is None:
        return []
    if end_dt < start_dt:
        start_dt, end_dt = end_dt, start_dt

    matches: List[LogMatch] = []
    for line in lines:
        if line.timestamp_dt is None:
            continue
        if start_dt <= line.timestamp_dt <= end_dt:
            matches.append(_as_match(line, "context"))
    return matches


def extract_context(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    line_number: int,
    before: int = 2,
    after: int = 2,
) -> List[LogMatch]:
    """Return nearby original lines around a matching line number."""
    lines = _ensure_lines(source)
    by_number = {line.line_number: line for line in lines}
    if line_number not in by_number:
        return []

    start = max(1, line_number - max(0, before))
    end = line_number + max(0, after)
    matches: List[LogMatch] = []
    for n in range(start, end + 1):
        line = by_number.get(n)
        if line is None:
            continue
        matches.append(
            _as_match(
                line,
                "context",
                anchor_line=line_number,
            )
        )
    return matches


def extract_request_ids(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
) -> List[LogMatch]:
    """Extract correlation/request IDs where present in the log text."""
    lines = _ensure_lines(source)
    matches: List[LogMatch] = []
    for line in lines:
        ids: List[str] = []
        ids.extend(REQUEST_ID_RE.findall(line.text))
        ids.extend(UUID_RE.findall(line.text))
        ids.extend(CHARGE_ID_RE.findall(line.text))
        # Deduplicate while preserving order
        seen: set[str] = set()
        unique_ids: List[str] = []
        for value in ids:
            if value not in seen:
                seen.add(value)
                unique_ids.append(value)
        if unique_ids:
            matches.append(_as_match(line, "pattern", request_ids=unique_ids))
    return matches


def collect_candidate_evidence(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
    relative_path: str = DEFAULT_RELATIVE_LOG,
    max_items: int = 24,
) -> List[Dict[str, Any]]:
    """Build a concise, de-duplicated candidate evidence bundle.

    Preference order: error, warning, burst, request-id pattern, error context.
    Excerpts are exact original lines. References use real 1-indexed line numbers.
    """
    lines = _ensure_lines(source)
    selected: Dict[int, LogMatch] = {}

    def _offer(match: LogMatch) -> None:
        existing = selected.get(match.line_number)
        if existing is None:
            selected[match.line_number] = match
            return
        rank = {"error": 0, "warning": 1, "burst": 2, "pattern": 3, "context": 4}
        if rank.get(match.match_type, 9) < rank.get(existing.match_type, 9):
            # Preserve extra burst metadata when replacing
            if existing.match_type == "burst" and "line_numbers" not in match.metadata:
                match.metadata.setdefault("superseded_burst", existing.metadata)
            selected[match.line_number] = match

    for match in find_error_lines(lines):
        _offer(match)
    for match in find_warning_lines(lines):
        _offer(match)
    for match in find_bursts(lines):
        _offer(match)
    for match in extract_request_ids(lines):
        _offer(match)

    # Add a little context around the first few errors so later agents see neighbours.
    error_anchors = [m.line_number for m in find_error_lines(lines)][:3]
    for anchor in error_anchors:
        for ctx in extract_context(lines, anchor, before=1, after=1):
            if ctx.line_number not in selected:
                _offer(ctx)

    ordered = sorted(selected.values(), key=lambda m: m.line_number)
    candidates = [m.to_candidate(relative_path) for m in ordered[:max_items]]

    counts = {
        "error": len(find_error_lines(lines)),
        "warning": len(find_warning_lines(lines)),
        "retry": count_pattern(lines, r"retry", ignore_case=True),
        "timeout": count_pattern(lines, r"timeout", ignore_case=True),
        "pool": count_pattern(lines, r"pool", ignore_case=True),
    }
    for item in candidates:
        item["pattern_counts"] = counts
    return candidates


def _ensure_lines(
    source: Union[str, Path, Sequence[str], Sequence[LogLine]],
) -> List[LogLine]:
    if not source:
        return []
    if isinstance(source, (list, tuple)) and source and isinstance(source[0], LogLine):
        return list(source)  # type: ignore[arg-type]
    return load_log_lines(source)  # type: ignore[arg-type]
