import threading
from typing import Dict

class InventoryManager:
    def __init__(self, initial_stock: int = 10):
        self.stock = initial_stock
        self.reservations = 0

    def get_stock(self) -> int:
        return self.stock

    def set_stock(self, val: int):
        self.stock = val

    def deduct_stock(self, quantity: int) -> bool:
        current = self.get_stock()
        if current >= quantity:
            self.set_stock(current - quantity)
            self.reservations += quantity
            return True
        return False
