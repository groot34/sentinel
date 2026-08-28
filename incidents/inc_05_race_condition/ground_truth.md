# Ground Truth: Incident 05 (Race Condition in Concurrent Inventory Counter)

## Root Cause
Commit `4b5a6c` replaced an atomic database decrement query (`UPDATE inventory SET stock = stock - 1 WHERE stock >= 1`) with an application-level check-then-act pattern (`if current >= qty: set_stock(current - qty)`). Under concurrent flash-sale requests across multiple worker threads, threads read the same stale inventory value before writing, causing 18 orders to be approved for an item with only 10 units in stock.

## Causal Chain
1. Code Change: Replaced atomic database lock with non-synchronized read-modify-write.
2. Concurrent Traffic: Multiple threads read `stock = 10` concurrently.
3. Race Condition: All threads evaluated condition as true and decremented stock independently.
4. Business Impact: Item was oversold by 8 units (final stock: -8).

## Distractor
Telemetry noted a 12ms replication delay on Redis secondary replica.

## Minimal Fix
Use atomic decrements at the database level (`stock = stock - :qty WHERE stock >= :qty`) or utilize distributed locks / mutexes around check-then-act blocks.

## Detection Test
`tests/test_service.py::test_inventory_check_and_deduct` tests standard decrement flow.
