from typing import List

class MockPool:
    def __init__(self, size: int = 10):
        self.size = size
        self.active = 0

    def acquire(self):
        if self.active >= self.size:
            raise TimeoutError("Connection pool exhausted!")
        self.active += 1
        return self

    def release(self):
        self.active = max(0, self.active - 1)

    def query(self, sql: str):
        return ["row1", "row2"]

class ReportGenerator:
    def __init__(self, pool: MockPool):
        self.pool = pool

    def generate_report(self, date_filter: str):
        conn = self.pool.acquire()
        rows = conn.query("SELECT * FROM metrics")
        if not date_filter.startswith("2026-"):
            raise ValueError(f"Unsupported date format: {date_filter}")
        conn.release()
        return rows
