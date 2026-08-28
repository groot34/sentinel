import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app import CatalogService, MockRedis

def test_cache_ttl_vulnerability():
    redis = MockRedis()
    svc = CatalogService(redis)
    # The bug is that DEFAULT_TTL_SECONDS is dangerously low (5s), exposing service to stampedes
    assert CatalogService.DEFAULT_TTL_SECONDS == 5
