# Ground Truth: Incident 03 (Kafka Consumer Lag from Slow Downstream Handler)

## Root Cause
Commit `7e8b1a` inserted a synchronous, blocking HTTP call to an external partner webhook (`self.http_client.post`) directly into the single-threaded message processing loop `handle_record()`. The external webhook has ~240ms latency, slashing consumer throughput from 1,250 msgs/sec to <18 msgs/sec. Incoming message volume quickly exceeded consumption capacity, causing consumer lag to climb to >140,000 records and triggering `max.poll.interval.ms` group rebalances.

## Causal Chain
1. Code Change: Synchronous HTTP POST placed directly in consumer loop.
2. Runtime Behavior: Per-record processing time increased from 0.8ms to 240ms.
3. Message Backlog: Consumer throughput collapsed below ingestion rate; lag exceeded 140k records.
4. Cluster Instability: Slow poll loop triggered Kafka consumer eviction and repeated rebalance storms.

## Distractor
Kafka broker 2 emitted a partition balance log advisory 15 minutes before the incident.

## Minimal Fix
Offload partner webhook dispatches to an asynchronous background worker pool (or emit to a dedicated outbound topic) so the Kafka ingestion loop remains non-blocking.

## Detection Test
`tests/test_service.py::test_handler_sync_webhook_detection` verifies webhook execution behavior.
