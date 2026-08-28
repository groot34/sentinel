import time
from typing import Dict, Any

class MockRedis:
    def __init__(self):
        self.store = {}
        self.expirations = {}

    def get(self, key: str):
        now = time.time()
        if key in self.expirations and now > self.expirations[key]:
            del self.store[key]
            del self.expirations[key]
            return None
        return self.store.get(key)

    def setex(self, key: str, ttl: int, value: Any):
        self.store[key] = value
        self.expirations[key] = time.time() + ttl

class CatalogService:
    DEFAULT_TTL_SECONDS = 5  # Misconfigured short TTL

    def __init__(self, redis_client: MockRedis):
        self.redis = redis_client
        self.db_query_count = 0

    def get_category_products(self, category_id: str):
        cache_key = f"category:{category_id}"
        cached = self.redis.get(cache_key)
        if cached is not None:
            return cached

        # Cache miss - heavy DB query
        self.db_query_count += 1
        data = {"category": category_id, "products": ["P100", "P101", "P102"]}
        self.redis.setex(cache_key, self.DEFAULT_TTL_SECONDS, data)
        return data
