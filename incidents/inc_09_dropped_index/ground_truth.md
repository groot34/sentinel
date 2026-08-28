# Ground Truth: Incident 09 (Missing Database Index After Migration)

## Root Cause
Commit `ab1c2d` applied migration `004_partition_items_table.sql`, creating partitioned tables but omitting the compound B-tree index `idx_tenant_status_name ON items(tenant_id, status, name)`. Every search filter query (`/api/v1/search?tenant_id=...&status=active`) switched from an index scan to a full sequential table scan across 2,000,000 rows, driving disk read throughput to 310MB/s and DB CPU to 99%.

## Causal Chain
1. Code Change: Table partitioning migration dropped compound index.
2. Query Planner: PostgreSQL planner reverted to `Seq Scan on items`.
3. Disk I/O Saturation: Read throughput spiked to 310 MB/s per node.
4. User Latency: P95 search API latency skyrocketed from 4ms to 2400ms.

## Distractor
Elasticsearch cluster reported yellow status due to an unassigned dev shard.

## Minimal Fix
Create the missing compound index concurrently:
`CREATE INDEX CONCURRENTLY idx_items_tenant_status ON items (tenant_id, status, name);`

## Detection Test
`tests/test_service.py::test_missing_index_causes_table_scan` detects table scan count behavior.
