# Ground Truth: Incident 02 (Redis Cache Stampede from TTL Misconfiguration)

## Root Cause
Commit `d219fa` reduced `DEFAULT_TTL_SECONDS` from 3600s to 5s in `CatalogCacheManager` without implementing cache locking, probabilistic early expiration (XFetch), or background refresh. Under high traffic, popular category keys expired every 5 seconds, causing hundreds of concurrent worker threads to experience cache misses at the exact same instant and blast the underlying Postgres database simultaneously.

## Causal Chain
1. Code Change: TTL set to 5s.
2. Runtime Behavior: Category keys expire every 5 seconds under continuous 200 RPS traffic.
3. Resource Saturation: Postgres connection pool exhausted from redundant heavy queries for identical data.
4. User Impact: Cache hit ratio plummeted from 98% to 11%; HTTP 503 errors on product listing pages.

## Distractor
Application logs reported deprecated image URL format warnings. This was benign frontend schema deprecation.

## Minimal Fix
Restore TTL to >= 300s (e.g. 3600s with jitter) or implement a single-flight mutex/probabilistic refresh for hot keys.

## Detection Test
`tests/test_service.py::test_cache_ttl_vulnerability` identifies the unsafe 5s TTL setting.
