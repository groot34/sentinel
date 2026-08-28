import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import CheckoutService, ExternalTaxService

def test_checkout_high_timeout_setting():
    tax_service = ExternalTaxService()
    service = CheckoutService(tax_service)
    assert CheckoutService.TIMEOUT_SECONDS == 60.0
