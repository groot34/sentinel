import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import SearchService, MockDatabase

def test_missing_index_causes_table_scan():
    db = MockDatabase()
    svc = SearchService(db)
    svc.search("tenant_1", "active")
    assert db.table_scan_count == 1, "Sequential table scan occurred due to missing index."
