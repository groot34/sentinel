# Ground Truth: Incident 07 (Retry Storm from Aggressive Policy Without Backoff)

## Root Cause
Commit `8f9a0b` configured `MAX_RETRIES = 10` and `BACKOFF_BASE_SECONDS = 0.0` in `PaymentClient`. When a minor 1% transient failure occurred at the downstream payment gateway, the payment client immediately hammered the gateway with 10 synchronous, zero-delay retries per failed request. This created a 10x traffic amplification (50 req/s -> 510 req/s), triggering gateway rate limits (HTTP 429) and total service degradation.

## Causal Chain
1. Code Change: Configured 10 retries with 0ms backoff and zero jitter.
2. Transient Glitch: Downstream gateway returned a single 503 status code.
3. Traffic Amplification: Client fired 10 immediate retries in tight loop for every request.
4. Cascading Collapse: Downstream gateway rate limiters triggered; all payments failed.

## Distractor
DNS resolver emitted a routine nameserver TTL renewal log.

## Minimal Fix
Set `MAX_RETRIES <= 3`, implement exponential backoff (`delay = base * 2 ** attempt`), and add randomized full jitter.

## Detection Test
`tests/test_service.py::test_payment_client_retry_count` verifies that client executes 10 immediate retries upon 503.
