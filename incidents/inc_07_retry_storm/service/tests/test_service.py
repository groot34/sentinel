import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import PaymentClient, MockGateway

def test_payment_client_retry_count():
    gateway = MockGateway()
    client = PaymentClient(gateway)
    client.execute_charge(100.0)
    assert gateway.call_count == 10
