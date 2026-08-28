import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import OrderEventHandler, MockHttpClient

def test_handler_sync_webhook_detection():
    client = MockHttpClient()
    handler = OrderEventHandler(client)
    # The bug is that handler calls synchronous post during record handling
    status = handler.handle_record({"event_id": 101}, is_async=False)
    assert status == 200
