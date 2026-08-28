from typing import Dict, Any, List

class MockLedgerDB:
    def __init__(self):
        self.indexes = set()  # Index dropped
        self.active_conns = 0
        self.max_conns = 15

    def acquire(self):
        if self.active_conns >= self.max_conns:
            raise TimeoutError("DB Pool Full")
        self.active_conns += 1
        return self

    def release(self):
        self.active_conns = max(0, self.active_conns - 1)

    def query_ledger(self, account_id: str) -> List[Dict[str, Any]]:
        if "idx_ledger_account_entry_date" in self.indexes:
            return [{"id": 1, "amount": 100.0}]
        return [{"id": 1, "amount": 100.0}]

class LedgerService:
    def __init__(self, db: MockLedgerDB):
        self.db = db

    def get_balance(self, account_id: str):
        conn = self.db.acquire()
        try:
            entries = self.db.query_ledger(account_id)
            return sum(e["amount"] for e in entries)
        finally:
            conn.release()
