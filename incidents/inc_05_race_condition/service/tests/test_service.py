import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import InventoryManager

def test_inventory_check_and_deduct():
    mgr = InventoryManager(initial_stock=5)
    success = mgr.deduct_stock(2)
    assert success is True
    assert mgr.get_stock() == 3
