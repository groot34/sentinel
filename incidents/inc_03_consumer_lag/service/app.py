from typing import Dict, Any

class MockHttpClient:
    def __init__(self, latency_ms: float = 240.0):
        self.latency_ms = latency_ms

    def post(self, url: str, json_data: Dict[str, Any], timeout: float):
        return type("Resp", (), {"status_code": 200})()

class OrderEventHandler:
    def __init__(self, http_client: MockHttpClient):
        self.http_client = http_client
        self.processed = []

    def store_analytics(self, event: Dict[str, Any]):
        self.processed.append(event)

    def handle_record(self, event: Dict[str, Any], is_async: bool = False):
        self.store_analytics(event)
        if not is_async:
            resp = self.http_client.post("https://partner-gateway.internal/events", json_data=event, timeout=10.0)
            return resp.status_code
        return 202
