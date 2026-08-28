from typing import List, Dict, Any

class MockDatabase:
    def __init__(self):
        self.indexes = set()  # Missing index
        self.table_scan_count = 0

    def search_items(self, tenant_id: str, status: str) -> List[Dict[str, Any]]:
        if "idx_tenant_status" in self.indexes:
            return [{"id": 1, "name": "Item A"}]
        self.table_scan_count += 1
        return [{"id": 1, "name": "Item A"}]

class SearchService:
    def __init__(self, db: MockDatabase):
        self.db = db

    def search(self, tenant_id: str, status: str):
        return self.db.search_items(tenant_id, status)
