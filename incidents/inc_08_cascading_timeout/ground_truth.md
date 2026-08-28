# Ground Truth: Incident 08 (Cascading Timeouts from Missing Circuit Breaker)

## Root Cause
Commit `9a0b1c` increased `TIMEOUT_SECONDS` on external tax service calls to 60.0s and disabled the circuit breaker. When the third-party tax calculation provider experienced slowdowns (25–30s response times), checkout worker threads remained blocked holding socket connections. This quickly exhausted the fixed 30-worker thread pool, causing incoming checkout traffic to back up and upstream gateways to drop connections with HTTP 504 Gateway Timeouts.

## Causal Chain
1. Code Change: Configured 60s timeout without circuit breaker or fallback tax table.
2. Downstream Latency: Third-party tax service latency degraded to 25s.
3. Thread Starvation: All 30 worker threads blocked waiting on tax API response.
4. Upstream Cascade: Ingress gateway hit 504 timeouts; checkout API unavailable.

## Distractor
Ingress proxy logged TLS certificate validity check confirmation.

## Minimal Fix
Set tax client timeout <= 2.0s, implement a Circuit Breaker (e.g., trip after 5 consecutive failures), and fallback to estimated default tax rates on failure.

## Detection Test
`tests/test_service.py::test_checkout_high_timeout_setting` inspects the timeout configuration.
