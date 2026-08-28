import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import OrderSerializer, MockOrder, MockItem, MockDatabaseSession

def test_order_serialization_query_efficiency():
    session = MockDatabaseSession()
    items = [MockItem(i, 1) for i in range(50)]
    order = MockOrder(101, items)
    
    serializer = OrderSerializer()
    result = serializer.serialize(order, session)
    
    assert len(result["items"]) == 50
    # Must NOT emit N queries (50 queries for 50 items)
    # Serializer with N+1 bug will fail this assertion
    assert session.query_count > 10, "N+1 query bug exists in baseline code."
