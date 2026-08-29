"""Deterministic verification tools — zero LLM calls, read-only.

Provides safe inspectors against the incident bundle (source/logs/metrics/diff)
that the Verification Agent can dispatch to programmatically test hypotheses.
"""

from __future__ import annotations

import ast
import csv
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

REQUIRED_STR = "Required"
UNSATISFIABLE_STR = "Unsatisfiable"

CHECK_PASS = "PASS"
CHECK_FAIL = "FAIL"
CHECK_INCONCLUSIVE = "INCONCLUSIVE"

VERDICT_CONFIRMED = "CONFIRMED"
VERDICT_REJECTED = "REJECTED"
VERDICT_INCONCLUSIVE = "INCONCLUSIVE"


@dataclass
class CheckResult:
    check_id: str
    description: str
    result: str
    evidence: List[str] = field(default_factory=list)
    reference: Optional[str] = None
    detail: Optional[str] = None


def _parse_source_path(incident_dir: Path, rel_ref: str) -> Optional[Tuple[Path, int, int]]:
    """Parse a service/app.py:N or service/app.py:N-M reference into path and line range.

    Returns (Path, start, end) where lines are 1-indexed and inclusive, or None.
    """
    m = re.match(r"^(service[/\\][^:]+):(\d+)(?:-(\d+))?$", rel_ref)
    if not m:
        return None
    rel = Path(m.group(1))
    start = int(m.group(2))
    end = int(m.group(3) or start)
    if start <= 0 or end < start:
        return None
    p = Path(incident_dir) / rel
    if not p.is_file():
        return None
    return p, start, end


def _read_lines(path: Path) -> List[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def _snippet_present_in_lines(snippet: str, lines: Sequence[str], start: int, end: int) -> bool:
    snippet_lines = [ln.rstrip() for ln in snippet.splitlines() if ln.strip()]
    if not snippet_lines:
        return False
    start_i = max(start - 1, 0)
    end_i = min(end, len(lines))
    window = [ln.rstrip() for ln in lines[start_i:end_i]]
    if not window:
        return False
    matches = 0
    i = 0
    j = 0
    while i < len(window) and j < len(snippet_lines):
        if snippet_lines[j] in window[i]:
            matches += 1
            j += 1
        i += 1
    return matches == len(snippet_lines)


def _any_snippet_in_source(snippet: str, source_lines: Sequence[str]) -> bool:
    snippet_lines = [ln.rstrip() for ln in snippet.splitlines() if ln.strip()]
    if not snippet_lines:
        return False
    i = 0
    j = 0
    while i < len(source_lines) and j < len(snippet_lines):
        if snippet_lines[j] in source_lines[i].rstrip():
            j += 1
        i += 1
    return j == len(snippet_lines)


# ---------- public source/logs/metrics query helpers ----------

def iter_service_py_files(incident_dir: Path) -> List[Path]:
    incident_dir = Path(incident_dir)
    service = incident_dir / "service"
    if not service.is_dir():
        return []
    out: List[Path] = []
    for p in sorted(service.rglob("*.py")):
        parts = p.parts
        if any(skip in parts for skip in ("__pycache__", ".pytest_cache", "tests")):
            continue
        out.append(p)
    return out


def read_application_log(incident_dir: Path) -> List[str]:
    log_path = Path(incident_dir) / "logs" / "application.log"
    if not log_path.is_file():
        return []
    return _read_lines(log_path)


def read_metrics_rows(incident_dir: Path) -> Tuple[List[str], List[Dict[str, Any]]]:
    csv_path = Path(incident_dir) / "metrics" / "metrics.csv"
    if not csv_path.is_file():
        return [], []
    with open(csv_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        headers = list(reader.fieldnames or [])
        rows: List[Dict[str, Any]] = []
        for r in reader:
            out: Dict[str, Any] = {}
            for k, v in (r or {}).items():
                out[k] = v
            rows.append(out)
    return headers, rows


# ---------- AST-based source checkers ----------

def count_calls_named(source: str, function_name_patterns: Iterable[str]) -> List[Dict[str, Any]]:
    patterns = [re.compile(p) for p in function_name_patterns]
    results: List[Dict[str, Any]] = []
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return results
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name: Optional[str] = None
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr
        if not name:
            continue
        for p in patterns:
            if p.search(name):
                results.append({"lineno": getattr(node, "lineno", None), "call": name})
                break
    return results


def contains_db_call_inside_loop(source: str) -> Optional[Dict[str, Any]]:
    """Return True if an AST-detectable DB-style call (.query_* OR .execute OR
    session.*) appears lexically inside a For/While block."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    DB_NAMES = {"query", "execute", "fetchone", "fetchall", "session", "db_session"}

    def is_db_call(node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Attribute):
                if fn.attr in DB_NAMES or any(d in fn.attr for d in DB_NAMES):
                    return fn.attr
            elif isinstance(fn, ast.Name):
                if fn.id in DB_NAMES:
                    return fn.id
        return None

    def in_loop(node: ast.AST, stack: List[ast.AST]) -> bool:
        return any(isinstance(s, (ast.For, ast.While, ast.AsyncFor)) for s in stack)

    found: List[Dict[str, Any]] = []

    def walk(cur: ast.AST, stack: List[ast.AST]) -> None:
        call_name = is_db_call(cur)
        if call_name and in_loop(cur, stack):
            found.append({"lineno": getattr(cur, "lineno", None), "call": call_name})
        stack.append(cur)
        for child in ast.iter_child_nodes(cur):
            walk(child, stack)
        stack.pop()

    walk(tree, [])
    if found:
        return {"found": True, "hits": found}
    return {"found": False, "hits": []}


def find_retry_constants(source: str) -> Optional[Dict[str, Any]]:
    """Return retry max constant values parsed from assignments (MAX_RETRIES / RETRIES / attempts)
    at both top level and inside class definitions."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    hits: List[Dict[str, Any]] = []

    def check_assign(target: ast.AST, value: ast.AST, lineno: int) -> None:
        if not isinstance(target, ast.Name):
            return
        if not re.search(r"(?i)(retry|max_retry|attempt|backoff)", target.id):
            return
        val: Any = None
        if isinstance(value, ast.Constant) and isinstance(value.value, (int, float)):
            val = value.value
        elif isinstance(value, ast.UnaryOp) and isinstance(value.operand, ast.Constant) and isinstance(value.operand.value, (int, float)):
            val = value.operand.value
        if val is not None:
            hits.append({"name": target.id, "value": val, "lineno": lineno})

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                check_assign(t, node.value, getattr(node, "lineno", None))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            check_assign(node.target, node.value or ast.Constant(value=None), getattr(node, "lineno", None))
    return {"hits": hits}


def find_backoff_zero(source: str) -> Optional[Dict[str, Any]]:
    hits: List[Dict[str, Any]] = []
    for lineno, line in enumerate(source.splitlines(), start=1):
        if re.search(r"(?i)backoff[^=\n]*=\s*0(?:\.0)?\b", line):
            hits.append({"lineno": lineno, "line": line.strip()})
    return {"hits": hits}


def find_unbounded_mutable_class_level(source: str) -> Optional[Dict[str, Any]]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    hits: List[Dict[str, Any]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            for stmt in node.body:
                names: List[ast.Name] = []
                value: Optional[ast.AST] = None
                if isinstance(stmt, ast.Assign):
                    for t in stmt.targets:
                        if isinstance(t, ast.Name):
                            names.append(t)
                    value = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                    names.append(stmt.target)
                    value = stmt.value
                if value is not None and isinstance(value, (ast.Dict, ast.List, ast.Set)):
                    for n in names:
                        hits.append({"name": n.id, "class": node.name, "lineno": getattr(stmt, "lineno", None)})
    return {"hits": hits}


def find_acquire_without_release(source: str) -> Optional[Dict[str, Any]]:
    """Detect pattern: acquire() increments but the file has no guaranteed release.

    - If the file contains no release() call name at all → pattern flagged.
    - If release() exists as a method on some class, but a function body acquires
      (self.X.acquire or X.acquire()) and does NOT call release() within a finally
      block reachable from that same function, the acquire call sites are reported
      as 'no_guaranteed_release'.
    """
    acquire_calls = count_calls_named(source, [r"^acquire$"])
    release_calls = count_calls_named(source, [r"^release$"])
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"acquire_count": len(acquire_calls), "release_count": len(release_calls), "has_pattern": len(acquire_calls) > 0 and len(release_calls) == 0, "no_guaranteed_release": []}
    no_guaranteed: List[Dict[str, Any]] = []
    ACQUIRE_RE = re.compile(r"^acquire$")
    RELEASE_RE = re.compile(r"^release$")

    def has_release(node: ast.AST) -> bool:
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                fn = child.func
                name = None
                if isinstance(fn, ast.Name):
                    name = fn.id
                elif isinstance(fn, ast.Attribute):
                    name = fn.attr
                if name and RELEASE_RE.match(name):
                    return True
        return False

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            acquires_in_func: List[int] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    fn = child.func
                    name = None
                    if isinstance(fn, ast.Name):
                        name = fn.id
                    elif isinstance(fn, ast.Attribute):
                        name = fn.attr
                    if name and ACQUIRE_RE.match(name):
                        acquires_in_func.append(getattr(child, "lineno", 0))
            if not acquires_in_func:
                continue
            # Recursively inspect all Try nodes inside the function and check if
            # any acquire-call site (by file-line range) is lexically enclosed by
            # a Try whose finally block or exception handler includes a release().
            enclosed = set()

            def _walk(cur: ast.AST) -> None:
                if isinstance(cur, ast.Try):
                    finally_release = any(has_release(s) for s in (cur.finalbody or []))
                    handler_release = any(
                        has_release(hstmt)
                        for h in (cur.handlers or [])
                        for hstmt in ast.iter_child_nodes(h)
                    ) or any(has_release(h) for h in (cur.handlers or []))
                    if finally_release or handler_release:
                        # Enclosing Try: mark all acquire lines that fall within
                        # the Try's line span (start_lineno..end_lineno inclusive)
                        try_start = getattr(cur, "lineno", 0)
                        try_end = getattr(cur, "end_lineno", try_start) or try_start
                        for al in acquires_in_func:
                            if try_start <= al <= try_end:
                                enclosed.add(al)
                for c in ast.iter_child_nodes(cur):
                    _walk(c)

            _walk(node)
            if len(enclosed) < len(acquires_in_func):
                no_guaranteed.append({
                    "func": node.name,
                    "lineno": getattr(node, "lineno", None),
                    "acquire_lines": [al for al in acquires_in_func if al not in enclosed],
                })
    pattern = len(acquire_calls) > 0 and (len(release_calls) == 0 or len(no_guaranteed) > 0)
    return {
        "acquire_count": len(acquire_calls),
        "release_count": len(release_calls),
        "has_pattern": pattern,
        "no_guaranteed_release": no_guaranteed,
    }


def find_drop_index_actual_change(incident_dir: Path) -> Optional[Dict[str, Any]]:
    """Check if git_diff.patch added or removed DROP INDEX lines (the SQL command, not just a comment)."""
    patch = Path(incident_dir) / "git_diff.patch"
    if not patch.is_file():
        return None
    lines = _read_lines(patch)
    added_sql_drops = 0
    removed_sql_drops = 0
    for ln in lines:
        stripped = ln.lstrip()
        if stripped.startswith("+DROP INDEX") or stripped.startswith("+DROP INDEX"):
            added_sql_drops += 1
        elif stripped.startswith("-DROP INDEX"):
            removed_sql_drops += 1
    return {"added_drop_index_sql": added_sql_drops, "removed_drop_index_sql": removed_sql_drops, "net_sql_drops": added_sql_drops - removed_sql_drops}


# ---------- logs/metrics checkers ----------

def count_log_errors_by_pattern(log_lines: Sequence[str], pattern: str) -> int:
    try:
        rex = re.compile(pattern)
    except re.error:
        rex = re.compile(re.escape(pattern))
    return sum(1 for ln in log_lines if rex.search(ln))


def metric_spike_order(rows: Sequence[Dict[str, Any]], metrics: Sequence[str], time_field: str = "timestamp") -> List[Tuple[str, str]]:
    """Return ordered list of (timestamp, metric) of the first sample that contains each metric's max."""
    ordered: List[Tuple[str, str]] = []
    for metric in metrics:
        best = None
        best_row: Optional[Dict[str, Any]] = None
        for r in rows:
            val = r.get(metric)
            if val in (None, ""):
                continue
            try:
                fval = float(val)
            except ValueError:
                continue
            if best is None or fval > best:
                best = fval
                best_row = r
        if best_row is not None:
            ordered.append((str(best_row.get(time_field, "")), metric))
    ordered.sort(key=lambda t: t[0])
    return ordered


def max_metric_value(rows: Sequence[Dict[str, Any]], metric: str) -> Optional[float]:
    best: Optional[float] = None
    for r in rows:
        v = r.get(metric)
        if v in (None, ""):
            continue
        try:
            fv = float(v)
        except ValueError:
            continue
        if best is None or fv > best:
            best = fv
    return best


# ---------- plan-classification to check dispatch ----------

KEYWORDS_QUERY_IN_LOOP = ("query inside loop", "loop.*query", "n+1", "query amplification", "for .* loop .* db", "serializer.*query")
KEYWORDS_RETRY_CONFIG = ("retry configuration", "max_retries", "backoff", "retry loop", "retry policy")
KEYWORDS_CONN_POOL = ("connection acquire", "release path", "acquire.*release", "connection pool", "connection leak")
KEYWORDS_INDEX_DROP = ("drop index", "index drop", "index presence", "sql index", "migration change")
KEYWORDS_UNBOUNDED_COLLECTION = ("unbounded", "memory leak", "mutable collection", "class level dict", "cache grow", "memory growth", "audi[t_]_trace", "registry")
KEYWORDS_CORRELATION = ("correlation", "before.*after", "precedes", "metric spike", "latency precedes", "error precedes", "order of spikes")
KEYWORDS_LOG_PATTERN = ("log pattern", "errors matching", "count .*log", "error count")
KEYWORDS_METRIC_VALUE = ("metric value", "max metric", "value of", "threshold metric")


def _match_plan_tokens(plan: str, keywords: Sequence[str]) -> bool:
    low = plan.lower()
    for k in keywords:
        if re.search(k, low):
            return True
    return False


def run_dispatch_check(
    check_id: str,
    incident_dir: Path,
    hypothesis_claim: str,
    plan_step: str,
    evidence_bundles: Dict[str, Dict[str, Any]],
    referenced_evidence: List[Dict[str, Any]],
) -> CheckResult:
    """Dispatch a plan step to deterministic checkers.

    Returns a CheckResult (PASS/FAIL/INCONCLUSIVE) with references to incident
    bundle evidence IDs when possible.
    """
    incident_dir = Path(incident_dir)
    plan_text = f"{hypothesis_claim}\n{plan_step}"

    source_files = iter_service_py_files(incident_dir)
    source_by_rel: Dict[str, List[str]] = {}
    for sf in source_files:
        try:
            rel = str(sf.relative_to(incident_dir)).replace("\\", "/")
        except ValueError:
            continue
        source_by_rel[rel] = _read_lines(sf)
    all_source = "\n".join("\n".join(ls) for ls in source_by_rel.values())

    # Helper: gather referenced evidence excerpts
    ref_snippets: List[str] = []
    for ev in referenced_evidence:
        exc = (ev.get("excerpt") or "").strip()
        if exc:
            ref_snippets.append(exc)

    # -------- Check 1: query inside loop / N+1 --------
    if _match_plan_tokens(plan_text, KEYWORDS_QUERY_IN_LOOP):
        per_file_hits: List[str] = []
        for sf in source_files:
            src = "\n".join(_read_lines(sf))
            if not src.strip():
                continue
            res = contains_db_call_inside_loop(src)
            if res and res.get("found"):
                for h in res.get("hits", []):
                    per_file_hits.append(f"{sf.relative_to(incident_dir)}:{h['lineno']} ({h['call']})")
        if per_file_hits:
            ref_ev = [e["evidence_id"] for e in referenced_evidence if e.get("evidence_id") and any(slug in (e.get("type") or "") + (e.get("excerpt") or "") for slug in ("loop", "query", "SELECT", "db_session"))]
            return CheckResult(
                check_id=check_id,
                description="AST scan detected database calls inside a for/while loop.",
                result=CHECK_PASS,
                evidence=ref_ev or [e["evidence_id"] for e in referenced_evidence[:3]],
                reference="; ".join(per_file_hits[:3]),
                detail="; ".join(per_file_hits),
            )
        return CheckResult(
            check_id=check_id,
            description="AST scan did not find DB call inside a loop.",
            result=CHECK_FAIL,
            evidence=[e["evidence_id"] for e in referenced_evidence[:2]],
            reference=None,
            detail="No DB-style call inside For/While AST nodes in service/**/*.py.",
        )

    # -------- Check 2: retry configuration (high max, zero backoff, retry loop) --------
    if _match_plan_tokens(plan_text, KEYWORDS_RETRY_CONFIG):
        retry_hits = find_retry_constants(all_source) or {"hits": []}
        backoff_hits = find_backoff_zero(all_source) or {"hits": []}
        retry_asts: List[str] = []
        try:
            for sf in source_files:
                src = "\n".join(_read_lines(sf))
                try:
                    tree = ast.parse(src)
                except SyntaxError:
                    continue
                for node in ast.walk(tree):
                    if isinstance(node, (ast.While, ast.For)):
                        text = ast.unparse(node) if sys.version_info >= (3, 9) else ""
                        if "attempt" in text or "retry" in text:
                            retry_asts.append(f"{sf.relative_to(incident_dir)}:{getattr(node, 'lineno', '?')}")
                if len(retry_asts) >= 2:
                    break
        except Exception:
            pass
        high_max = [h for h in retry_hits["hits"] if isinstance(h.get("value"), (int, float)) and h["value"] >= 5]
        backoff_zero = backoff_hits.get("hits", [])
        passed = (len(high_max) > 0) or (len(backoff_zero) > 0) or (len(retry_asts) > 0)
        if passed:
            details = []
            if high_max:
                details.append(f"high retry max: {high_max}")
            if backoff_zero:
                details.append(f"zero backoff: {backoff_zero}")
            if retry_asts:
                details.append(f"retry loops: {retry_asts}")
            return CheckResult(
                check_id=check_id,
                description="Aggressive retry configuration detected (high MAX_RETRIES or zero backoff or retry loop AST).",
                result=CHECK_PASS,
                evidence=[e["evidence_id"] for e in referenced_evidence],
                reference=retry_asts[0] if retry_asts else (high_max[0]["lineno"] if high_max else (backoff_zero[0]["lineno"] if backoff_zero else None)),
                detail=" | ".join(details),
            )
        return CheckResult(
            check_id=check_id,
            description="Retry configuration appears moderate: no high MAX_RETRIES or zero backoff, no retry loop AST.",
            result=CHECK_FAIL,
            evidence=[e["evidence_id"] for e in referenced_evidence],
            detail=str({"retry_hits": retry_hits, "backoff": backoff_hits}),
        )

    # -------- Check 3: acquire without release --------
    if _match_plan_tokens(plan_text, KEYWORDS_CONN_POOL):
        findings: List[Dict[str, Any]] = []
        for sf in source_files:
            src = "\n".join(_read_lines(sf))
            res = find_acquire_without_release(src)
            if res and res.get("has_pattern"):
                findings.append({"file": str(sf.relative_to(incident_dir)), "detail": res})
        if findings:
            refs = [f"{f['file']}" for f in findings[:3]]
            return CheckResult(
                check_id=check_id,
                description="Source file contains acquire() without release() call (AST name-count heuristic).",
                result=CHECK_PASS,
                evidence=[e["evidence_id"] for e in referenced_evidence if e.get("evidence_id")],
                reference="; ".join(refs),
                detail=json.dumps(findings, default=str),
            )
        return CheckResult(
            check_id=check_id,
            description="Every acquire() in inspected files has a corresponding release().",
            result=CHECK_FAIL,
            evidence=[e["evidence_id"] for e in referenced_evidence],
        )

    # -------- Check 4: DROP INDEX actual SQL change --------
    if _match_plan_tokens(plan_text, KEYWORDS_INDEX_DROP):
        r = find_drop_index_actual_change(incident_dir)
        if r is None:
            return CheckResult(check_id, "No git_diff.patch available; cannot evaluate index drop claim.", CHECK_INCONCLUSIVE, [e["evidence_id"] for e in referenced_evidence])
        net_sql = r.get("net_sql_drops", 0)
        if net_sql == 0:
            return CheckResult(
                check_id=check_id,
                description="git_diff.patch does not change the DROP INDEX SQL command count (comment-only or cosmetic change).",
                result=CHECK_FAIL,
                evidence=[e["evidence_id"] for e in referenced_evidence],
                reference="git_diff.patch",
                detail=json.dumps(r),
            )
        return CheckResult(
            check_id=check_id,
            description=f"git_diff.patch modified the DROP INDEX SQL line count (net {net_sql:+d}).",
            result=CHECK_PASS,
            evidence=[e["evidence_id"] for e in referenced_evidence],
            reference="git_diff.patch",
            detail=json.dumps(r),
        )

    # -------- Check 5: unbounded mutable collection --------
    if _match_plan_tokens(plan_text, KEYWORDS_UNBOUNDED_COLLECTION):
        findings: List[Dict[str, Any]] = []
        for sf in source_files:
            src = "\n".join(_read_lines(sf))
            r = find_unbounded_mutable_class_level(src)
            if r and r.get("hits"):
                for h in r["hits"]:
                    h["file"] = str(sf.relative_to(incident_dir))
                    findings.append(h)
            # Also check for ` = {} ` or ` = []` at module level with accumulation in functions
            src_lines = src.splitlines()
            for i, line in enumerate(src_lines, start=1):
                # assignment to bare name with dict/list literal
                if re.search(r"^\s*[A-Z_][A-Z0-9_]*\s*=\s*(?:\{|\[\])", line) and not line.strip().startswith("class ") and not line.strip().startswith("def "):
                    findings.append({"file": str(sf.relative_to(incident_dir)), "lineno": i, "name": line.strip()})
        if findings:
            refs = [f"{f.get('file','')}{':' + str(f['lineno']) if f.get('lineno') else ''}" for f in findings[:3]]
            return CheckResult(
                check_id=check_id,
                description="Source contains class-level or module-level mutable collection (dict/list) that may grow unboundedly.",
                result=CHECK_PASS,
                evidence=[e["evidence_id"] for e in referenced_evidence],
                reference="; ".join(refs),
                detail=json.dumps(findings, default=str)[:400],
            )
        return CheckResult(
            check_id=check_id,
            description="No unbounded mutable collection found at class/module level in source files.",
            result=CHECK_FAIL,
            evidence=[e["evidence_id"] for e in referenced_evidence],
        )

    # -------- Check 6: metric spike order / correlation --------
    if _match_plan_tokens(plan_text, KEYWORDS_CORRELATION):
        _, rows = read_metrics_rows(incident_dir)
        candidate_metrics = [
            "active_db_conns", "ledger_query_duration_ms", "order_query_duration_ms",
            "client_retries_sec", "error_rate_pct", "pod_readiness_failures",
            "cache_size_bytes", "retry_count", "db_query_duration_ms",
        ]
        order = metric_spike_order(rows, candidate_metrics)
        if order:
            detail = " | ".join(f"{t}:{m}" for t, m in order)
            return CheckResult(
                check_id=check_id,
                description="Metric spike order reconstructed from metrics.csv.",
                result=CHECK_PASS if len(order) >= 2 else CHECK_INCONCLUSIVE,
                evidence=[e["evidence_id"] for e in referenced_evidence if e.get("source") == "metrics"],
                reference="metrics/metrics.csv",
                detail=detail,
            )
        return CheckResult(
            check_id=check_id,
            description="No candidate metric spikes could be ordered.",
            result=CHECK_INCONCLUSIVE,
            evidence=[e["evidence_id"] for e in referenced_evidence],
        )

    # -------- Check 7: log pattern count --------
    if _match_plan_tokens(plan_text, KEYWORDS_LOG_PATTERN):
        logs = read_application_log(incident_dir)
        pattern_extracted: List[str] = re.findall(r"[\"“]([^\"“”]{2,})[\"”]", plan_text)
        counts: Dict[str, int] = {}
        for token in (pattern_extracted or ["ERROR", "WARN", "retry", "timeout", "pool exhausted", "pool empty"]):
            counts[token] = count_log_errors_by_pattern(logs, re.escape(token))
        if sum(counts.values()) > 0:
            return CheckResult(
                check_id=check_id,
                description="Matching log lines exist for extracted keywords.",
                result=CHECK_PASS,
                evidence=[e["evidence_id"] for e in referenced_evidence if e.get("source") == "logs"],
                reference="logs/application.log",
                detail=json.dumps(counts),
            )
        return CheckResult(
            check_id=check_id,
            description="No matching log lines found for keywords.",
            result=CHECK_FAIL,
            evidence=[e["evidence_id"] for e in referenced_evidence],
        )

    # -------- Check 8: metric value / threshold --------
    if _match_plan_tokens(plan_text, KEYWORDS_METRIC_VALUE):
        _, rows = read_metrics_rows(incident_dir)
        # extract metric name candidate from hypothesis or plan
        candidate = None
        for metric in ["error_rate_pct", "client_retries_sec", "active_db_conns", "ledger_query_duration_ms", "order_query_duration_ms", "pod_readiness_failures", "cache_size_bytes", "db_query_duration_ms"]:
            if re.search(re.escape(metric), plan_text, re.IGNORECASE):
                candidate = metric
                break
        if candidate:
            val = max_metric_value(rows, candidate)
            if val is not None:
                return CheckResult(
                    check_id=check_id,
                    description=f"Max value of metric {candidate} = {val} (read from metrics.csv).",
                    result=CHECK_PASS,
                    evidence=[e["evidence_id"] for e in referenced_evidence if e.get("metric") == candidate or candidate in (e.get("excerpt") or "") + (e.get("interpretation") or "")],
                    reference="metrics/metrics.csv",
                    detail=json.dumps({"metric": candidate, "max": val}),
                )
        return CheckResult(
            check_id=check_id,
            description="Could not extract a named metric from the plan step; metric value check not run.",
            result=CHECK_INCONCLUSIVE,
            evidence=[e["evidence_id"] for e in referenced_evidence],
        )

    # -------- Fallback: referenced-evidence snippet grounding against real source --------
    # Attempt to verify that at least one referenced evidence excerpt is actually present
    grounded = 0
    grounded_refs: List[str] = []
    for ev in referenced_evidence:
        ref = ev.get("reference") or ""
        exc = (ev.get("excerpt") or "").strip()
        source_name: Optional[str] = None
        if ref.startswith("service/") and ":" in ref:
            m = re.match(r"^(service/[^:]+):(\d+)(?:-(\d+))?$", ref)
            if m:
                source_name = m.group(1)
                start = int(m.group(2))
                end = int(m.group(3) or start)
                lines = source_by_rel.get(source_name)
                if lines and _snippet_present_in_lines(exc, lines, start, end):
                    grounded += 1
                    grounded_refs.append(ref)
        elif ref.startswith("git_diff.patch"):
            patch = incident_dir / "git_diff.patch"
            if patch.is_file():
                patch_lines = _read_lines(patch)
                if any(any(sl in line for line in patch_lines) for sl in [ln for ln in exc.splitlines() if ln.strip()]):
                    grounded += 1
                    grounded_refs.append(ref)
        elif ref.startswith("logs/"):
            log_lines = read_application_log(incident_dir)
            if any(any(sl in line for line in log_lines) for sl in [ln for ln in exc.splitlines() if ln.strip()]):
                grounded += 1
                grounded_refs.append(ref)
        elif ref.startswith("metrics/"):
            _, rows = read_metrics_rows(incident_dir)
            if rows:
                grounded += 1
                grounded_refs.append(ref)
    if grounded > 0:
        return CheckResult(
            check_id=check_id,
            description="Referenced evidence excerpts verified against source/logs/metrics files (grounding fallback).",
            result=CHECK_PASS if grounded >= 1 else CHECK_INCONCLUSIVE,
            evidence=[e["evidence_id"] for e in referenced_evidence if e.get("evidence_id")],
            reference="; ".join(grounded_refs) or None,
            detail=f"{grounded} of {len(referenced_evidence)} referenced evidence items grounded.",
        )
    return CheckResult(
        check_id=check_id,
        description="Plan step could not be mapped to a deterministic check and referenced evidence was not groundable.",
        result=CHECK_INCONCLUSIVE,
        evidence=[e["evidence_id"] for e in referenced_evidence],
    )
