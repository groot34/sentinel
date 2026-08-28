import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import LedgerService, MockLedgerDB

def test_ledger_balance_check():
    db = MockLedgerDB()
    svc = LedgerService(db)
    balance = svc.get_balance("ACC_001")
    assert balance == 100.0
    assert db.active_conns == 0
