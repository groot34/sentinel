import time

class ExternalTaxService:
    def __init__(self, latency: float = 0.01):
        self.latency = latency

    def calculate_tax(self, amount: float):
        time.sleep(self.latency)
        return amount * 0.08

class CheckoutService:
    TIMEOUT_SECONDS = 60.0

    def __init__(self, tax_service: ExternalTaxService):
        self.tax_service = tax_service
        self.active_workers = 0

    def process_checkout(self, cart_id: str, amount: float):
        self.active_workers += 1
        try:
            tax = self.tax_service.calculate_tax(amount)
            return {"cart_id": cart_id, "total": amount + tax, "status": "APPROVED"}
        finally:
            self.active_workers -= 1
