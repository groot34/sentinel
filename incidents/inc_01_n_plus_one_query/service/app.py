from typing import List, Dict, Any

class MockAddress:
    def __init__(self, addr_id: int, city: str):
        self.id = addr_id
        self.city = city

class MockItem:
    def __init__(self, item_id: int, shipping_address_id: int):
        self.id = item_id
        self.shipping_address_id = shipping_address_id

class MockOrder:
    def __init__(self, order_id: int, items: List[MockItem]):
        self.id = order_id
        self.total = 100.0
        self.items = items

class MockDatabaseSession:
    def __init__(self):
        self.query_count = 0
        self.addresses = {1: MockAddress(1, "San Francisco"), 2: MockAddress(2, "New York")}

    def query_address_by_id(self, address_id: int):
        self.query_count += 1
        return self.addresses.get(address_id)

    def query_addresses_batch(self, address_ids: List[int]):
        self.query_count += 1
        return {aid: self.addresses.get(aid) for aid in address_ids if aid in self.addresses}

class OrderSerializer:
    def serialize(self, order: MockOrder, db_session: MockDatabaseSession) -> Dict[str, Any]:
        data = {
            "id": order.id,
            "total": order.total,
            "item_count": len(order.items),
            "items": []
        }
        for item in order.items:
            address = db_session.query_address_by_id(item.shipping_address_id)
            data["items"].append({"item_id": item.id, "shipping_to": address.city if address else None})
        return data
