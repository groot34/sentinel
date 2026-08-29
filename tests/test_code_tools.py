"""Unit tests for deterministic code/diff analysis tools.

No LLM calls. References must match original source/diff contents.
"""

from pathlib import Path

from agents.code_tools import (
    collect_candidate_evidence,
    detect_suspicious_patterns,
    extract_added_lines,
    extract_hunks,
    extract_removed_lines,
    get_source_context,
    iter_source_files,
    list_changed_files,
    load_git_diff,
    parse_git_diff,
    search_source,
)


INCIDENTS_DIR = Path(__file__).parent.parent / "incidents"
INC_01_DIR = INCIDENTS_DIR / "inc_01_n_plus_one_query"
INC_04_DIR = INCIDENTS_DIR / "inc_04_memory_leak"
INC_07_DIR = INCIDENTS_DIR / "inc_07_retry_storm"
INC_10_DIR = INCIDENTS_DIR / "inc_10_multi_symptom_cascade"
INC_09_DIR = INCIDENTS_DIR / "inc_09_dropped_index"

SAMPLE_DIFF = """diff --git a/service/example.py b/service/example.py
index a1b2c3d..e4f5g6h 100644
--- a/service/example.py
+++ b/service/example.py
@@ -10,8 +10,12 @@ class Example:
     def process(self, items, db):
         total = 0
-        for item in items:
-            total += item.price
+        for item in items:
+            row = db.query("SELECT * FROM prices WHERE id = ?", (item.id,))
+            total += row[0]["price"]
+            cache[item.id] = row  # unbounded global
         return total
"""


def test_load_git_diff_from_file():
    diff = load_git_diff(INC_01_DIR / "git_diff.patch")
    assert diff.text
    assert len(diff.hunks) >= 1
    assert "service/order_serializer.py" in diff.changed_files


def test_parse_git_diff_from_string():
    diff = parse_git_diff(SAMPLE_DIFF)
    assert len(diff.hunks) == 1
    assert diff.changed_files == ["service/example.py"]
    hunk = diff.hunks[0]
    assert hunk.old_start == 10
    assert hunk.new_start == 10
    assert hunk.old_path == "service/example.py"
    assert hunk.new_path == "service/example.py"


def test_changed_file_extraction_works():
    diff = load_git_diff(INC_01_DIR / "git_diff.patch")
    files = list_changed_files(diff)
    assert isinstance(files, list)
    assert "service/order_serializer.py" in files


def test_added_line_extraction_works():
    diff = load_git_diff(INC_01_DIR / "git_diff.patch")
    added = extract_added_lines(diff)
    assert len(added) >= 5
    for item in added:
        assert "file" in item
        assert "new_line" in item
        assert "text" in item
        assert "hunk_index" in item
    texts = [a["text"].strip() for a in added]
    assert any("for item in order.items" in t for t in texts)
    assert any("db_session.query_address_by_id" in t for t in texts)


def test_removed_line_extraction_works():
    diff = load_git_diff(INC_01_DIR / "git_diff.patch")
    removed = extract_removed_lines(diff)
    assert len(removed) >= 1
    for item in removed:
        assert "file" in item
        assert "old_line" in item
        assert "text" in item
        assert "hunk_index" in item
    texts = [r["text"].strip() for r in removed]
    assert any('"item_count": len(order.items)' in t for t in texts)


def test_hunk_parsing_returns_hunks_with_metadata():
    diff = load_git_diff(INC_07_DIR / "git_diff.patch")
    hunks = extract_hunks(diff)
    assert len(hunks) >= 1
    hunk = hunks[0]
    assert hunk.hunk_index >= 1
    assert hunk.new_path == "service/payment_client.py"
    assert hunk.old_path == "service/payment_client.py"
    assert len(hunk.added) >= 2
    assert len(hunk.removed) >= 2
    patch_texts = [t for _, _, t in hunk.added]
    assert any("MAX_RETRIES = 10" in t for t in patch_texts)
    assert any("BACKOFF_BASE_SECONDS = 0.0" in t for t in patch_texts)


def test_source_search_finds_real_lines():
    hits = search_source(INC_01_DIR, "query_address_by_id")
    assert len(hits) >= 1
    for hit in hits:
        rel_path = INC_01_DIR / hit.relative_path
        assert rel_path.exists()
        lines = rel_path.read_text(encoding="utf-8").splitlines()
        assert 1 <= hit.line_number <= len(lines)
        assert lines[hit.line_number - 1] == hit.text


def test_source_search_regex_and_case():
    hits = search_source(INC_07_DIR, r"MAX_RETRIES\s*=")
    assert len(hits) >= 1

    ignore = search_source(INC_01_DIR, "ORDER", ignore_case=True)
    assert len(ignore) >= 1


def test_source_context_extraction_returns_real_neighbors():
    hits = search_source(INC_01_DIR, "class OrderSerializer")
    assert hits, "Expected OrderSerializer definition in incident 01 source"
    anchor = hits[0]
    context = get_source_context(
        INC_01_DIR, anchor.relative_path, anchor.line_number, before=1, after=3
    )
    assert len(context) >= 4
    lines = (INC_01_DIR / context[0].relative_path).read_text(
        encoding="utf-8", errors="ignore"
    ).splitlines()
    for ctx in context:
        assert 1 <= ctx.line_number <= len(lines)
        assert lines[ctx.line_number - 1] == ctx.text
    numbers = [c.line_number for c in context]
    assert numbers[0] == max(1, anchor.line_number - 1)


def test_source_context_missing_file_returns_empty():
    empty = get_source_context(INC_01_DIR, "service/does_not_exist.py", 1)
    assert empty == []


def test_suspicious_pattern_detection_finds_query_in_loop_inc01():
    diff = load_git_diff(INC_01_DIR / "git_diff.patch")
    findings = detect_suspicious_patterns(INC_01_DIR, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "query_or_db_call_inside_loop" in patterns or "query_inside_added_loop" in patterns

    for f in findings:
        assert f.source in {"code", "git_diff", "config"}
        assert f.reference
        assert f.excerpt
        if f.source == "code":
            assert ":" in f.reference
            parts = f.reference.split(":")
            rel = parts[0]
            path = INC_01_DIR / rel
            assert path.exists(), f"Source reference does not exist: {f.reference}"


def test_suspicious_pattern_detection_finds_global_collection_inc04():
    diff = load_git_diff(INC_04_DIR / "git_diff.patch")
    findings = detect_suspicious_patterns(INC_04_DIR, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "class_level_mutable_collection" in patterns or "unbounded_collection" in patterns


def test_suspicious_pattern_detection_finds_high_retry_and_zero_backoff_inc07():
    diff = load_git_diff(INC_07_DIR / "git_diff.patch")
    findings = detect_suspicious_patterns(INC_07_DIR, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "high_retry_count" in patterns
    assert "zero_backoff" in patterns


def test_suspicious_pattern_detection_finds_drop_index_incidents():
    diff09 = load_git_diff(INC_09_DIR / "git_diff.patch")
    findings09 = detect_suspicious_patterns(INC_09_DIR, diff09)
    patterns09 = [f.metadata.get("pattern", "") for f in findings09]
    assert "drop_index" in patterns09 or "index_creation_removed" in patterns09

    diff10 = load_git_diff(INC_10_DIR / "git_diff.patch")
    findings10 = detect_suspicious_patterns(INC_10_DIR, diff10)
    patterns10 = [f.metadata.get("pattern", "") for f in findings10]
    assert "drop_index" in patterns10


def test_suspicious_pattern_detection_finds_connection_release_inc06():
    inc06 = INCIDENTS_DIR / "inc_06_connection_exhaustion"
    diff = load_git_diff(inc06 / "git_diff.patch")
    findings = detect_suspicious_patterns(inc06, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "connection_acquire_without_guaranteed_release" in patterns


def test_suspicious_pattern_detection_finds_short_ttl_inc02():
    inc02 = INCIDENTS_DIR / "inc_02_cache_stampede"
    diff = load_git_diff(inc02 / "git_diff.patch")
    findings = detect_suspicious_patterns(inc02, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "short_cache_ttl" in patterns


def test_suspicious_pattern_detection_finds_long_timeout_inc08():
    inc08 = INCIDENTS_DIR / "inc_08_cascading_timeout"
    diff = load_git_diff(inc08 / "git_diff.patch")
    findings = detect_suspicious_patterns(inc08, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "long_timeout" in patterns


def test_suspicious_pattern_detection_finds_http_call_inc03():
    inc03 = INCIDENTS_DIR / "inc_03_consumer_lag"
    diff = load_git_diff(inc03 / "git_diff.patch")
    findings = detect_suspicious_patterns(inc03, diff)
    patterns = [f.metadata.get("pattern", "") for f in findings]
    assert "outbound_http_call" in patterns


def test_no_fabricated_line_numbers_in_evidence():
    inc_dirs = [INC_01_DIR, INC_04_DIR, INC_07_DIR, INC_10_DIR, INC_09_DIR]
    for inc_dir in inc_dirs:
        diff_path = inc_dir / "git_diff.patch"
        if not diff_path.exists():
            continue
        diff = load_git_diff(diff_path)
        candidates = collect_candidate_evidence(inc_dir)
        assert len(candidates) > 0, f"No candidates for {inc_dir.name}"
        for c in candidates:
            ref = c["reference"]
            assert ref, f"Empty reference for {inc_dir.name}"
            if c["source"] == "code":
                file_part, _, line_part = ref.partition(":")
                path = inc_dir / file_part
                assert path.exists(), f"Missing file in {inc_dir.name}: {file_part}"
                src_lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
                if "-" in line_part:
                    start_s, _, end_s = line_part.partition("-")
                    start = int(start_s)
                    end = int(end_s)
                    assert 1 <= start <= len(src_lines)
                    assert 1 <= end <= len(src_lines)
                else:
                    line = int(line_part)
                    assert 1 <= line <= len(src_lines), f"Bad line ref: {ref}"
            elif c["source"] == "git_diff":
                diff_lines = diff_path.read_text(encoding="utf-8").splitlines()
                assert "hunk" in ref or ":" in ref
                if "hunk" in ref:
                    hunk_no = int(ref.rsplit(" ", 1)[1])
                    assert 1 <= hunk_no <= len(diff.hunks)


def test_empty_diff_handled(tmp_path: Path):
    empty_dir = tmp_path / "inc_empty"
    (empty_dir / "service").mkdir(parents=True, exist_ok=True)
    (empty_dir / "service" / "app.py").write_text("print('hello')\n", encoding="utf-8")
    (empty_dir / "git_diff.patch").write_text("", encoding="utf-8")
    diff = load_git_diff(empty_dir / "git_diff.patch")
    assert diff.hunks == []
    assert list_changed_files(diff) == []
    candidates = collect_candidate_evidence(empty_dir)
    assert isinstance(candidates, list)


def test_missing_source_handled(tmp_path: Path):
    no_source = tmp_path / "inc_nosrc"
    no_source.mkdir()
    (no_source / "git_diff.patch").write_text(SAMPLE_DIFF, encoding="utf-8")
    files = iter_source_files(no_source)
    assert files == []


def test_iter_source_files_excludes_tests_and_pycache(tmp_path: Path):
    root = tmp_path / "inc_src"
    svc = root / "service"
    svc.mkdir(parents=True)
    (svc / "app.py").write_text("x = 1\n", encoding="utf-8")
    tests = svc / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("pass\n", encoding="utf-8")
    pycache = svc / "__pycache__"
    pycache.mkdir()
    (pycache / "app.cpython-312.pyc").write_bytes(b"abc")
    found = iter_source_files(root)
    names = [p.name for p in found]
    assert "app.py" in names
    assert "test_x.py" not in names
    assert "app.cpython-312.pyc" not in names


def test_candidate_evidence_is_deterministic_on_repeated_runs():
    first = collect_candidate_evidence(INC_01_DIR)
    second = collect_candidate_evidence(INC_01_DIR)
    assert len(first) == len(second)
    for a, b in zip(first, second):
        assert a["reference"] == b["reference"]
        assert a["type"] == b["type"]
        assert a["excerpt"] == b["excerpt"]


def test_no_hardcoded_incident_ids_in_code_tools():
    source = (Path(__file__).parent.parent / "agents" / "code_tools.py").read_text(
        encoding="utf-8"
    )
    for inc_id in [f"inc_{i:02d}" for i in range(1, 11)]:
        assert inc_id not in source, f"Hardcoded incident id {inc_id} in code_tools.py"


def test_candidate_evidence_types_are_from_schema_enum():
    for inc_dir in [INC_01_DIR, INC_04_DIR, INC_07_DIR, INC_10_DIR]:
        candidates = collect_candidate_evidence(inc_dir)
        for c in candidates:
            assert c["type"] in {
                "added_code",
                "removed_code",
                "suspicious_pattern",
                "changed_config",
            }
            assert c["source"] in {"git_diff", "code", "config"}
