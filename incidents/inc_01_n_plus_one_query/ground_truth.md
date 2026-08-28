# Ground Truth: Incident 01 (N+1 Database Query in Order API)

## Root Cause
In commit `c84a1f`, `OrderSerializer.serialize()` was modified to fetch shipping addresses individually inside a `for item in order.items` loop using `db_session.query_address_by_id()`. For orders with multiple items, this generates N queries per order in addition to the initial order query (N+1 query pattern). When bulk endpoints (`/api/v1/orders/bulk`) were requested, total database queries spiked to >250 per request, saturating the 20-connection pool and driving Postgres CPU to >98%.

## Causal Chain
1. Code Change: Serializer loop performs individual address queries per item.
2. Runtime Behavior: Bulk order requests execute hundreds of synchronous SQL queries sequentially.
3. Resource Saturation: Connection pool (size 20) is depleted, query queue depth surges.
4. User Impact: HTTP 504 Gateway Timeouts and HTTP 500 Connection Timeout exceptions on all order endpoints.

## Distractor
Logs noted intermittent TCP retransmissions on replica interface `bond0`. This was background network jitter and unrelated to query exhaustion on primary DB.

## Minimal Fix
Fetch unique address IDs upfront and execute a single batch query (`query_addresses_batch`), mapping results in-memory.

## Detection Test
`tests/test_service.py::test_order_serialization_query_efficiency` demonstrates that query count scales with items instead of remaining O(1).
