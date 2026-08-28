from typing import Dict, Any

class MockGateway:
    def __init__(self):
        self.call_count = 0

    def charge(self, amount: float):
        self.call_count += 1
        if self.call_count <= 10:
            return {"status": 503, "error": "Temporarily Unavailable"}
        return {"status": 200, "charge_id": "ch_success"}

class PaymentClient:
    MAX_RETRIES = 10
    BACKOFF_BASE_SECONDS = 0.0

    def __init__(self, gateway: MockGateway):
        self.gateway = gateway

    def execute_charge(self, amount: float):
        attempts = 0
        while attempts < self.MAX_RETRIES:
            attempts += 1
            resp = self.gateway.charge(amount)
            if resp.get("status") == 200:
                return resp
        return {"status": 503, "error": "Max retries exceeded"}
