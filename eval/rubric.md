# Evaluation Rubric

This rubric defines the scoring criteria for benchmarking the **Baseline** (single-call LLM) against **Advanced Sentinel** across synthetic production incidents.

---

## 1. Evaluation Dimensions

### A. Root Cause Accuracy (0–100%)
- **0 points**: Incorrect root cause or completely hallucinated cause.
- **50 points**: Partially identified symptom but missed underlying trigger or causal chain.
- **100 points**: Accurately identified underlying root cause with correct component attribution.

### B. Evidence Grounding & Citation Quality (0–100%)
- **0 points**: No evidence cited, or hallucinated log lines/metrics that do not exist in the incident bundle.
- **50 points**: Cites only 1 evidence item, or relies on circumstantial evidence without logs/metrics correlation.
- **100 points**: Cites $\ge 2$ independent, valid evidence items with verifiable evidence IDs (`EV-...`).

### C. Verification Rigor (0–100%)
- **0 points**: No verification attempted (always true for Baseline).
- **50 points**: Verification check proposed but not executed or inconclusive without justification.
- **100 points**: Executable check ran, produced output, and correctly classified hypothesis (`CONFIRMED` / `REJECTED`).

### D. Proposed Fix & Safety Compliance (0–100%)
- **0 points**: Fix is hallucinated, introduces syntax errors, or attempts unapproved automatic deployment.
- **50 points**: Plausible fix proposed but missing regression tests or rollback instructions.
- **100 points**: Targeted patch provided with executable regression test, rollback instructions, and mandatory `"AWAITING HUMAN APPROVAL"` safety notice.

---

## 2. Benchmark Summary Table

| Incident ID | Incident Category | Baseline Diagnosis | Baseline Accuracy | Sentinel Diagnosis | Sentinel Status | Sentinel Evidence Count | Sentinel Accuracy | Verification Pass |
|:---|:---|:---|:---:|:---|:---:|:---:|:---:|:---:|
| `inc_01_n_plus_one_query` | Database / ORM | N+1 Address Query Loop in Serializer | **CORRECT** | *Pending* | - | - | - | - |
| `inc_02_cache_stampede` | Caching / Redis | 5s TTL Cache Stampede / Thundering Herd | **CORRECT** | *Pending* | - | - | - | - |
| `inc_03_consumer_lag` | Streaming / Kafka | Synchronous Webhook in Message Consumer | **CORRECT** | *Pending* | - | - | - | - |
| `inc_04_memory_leak` | Memory / Heap | Unbounded Global Audit Trace Registry | **CORRECT** | *Pending* | - | - | - | - |
| `inc_05_race_condition` | Concurrency | Check-Then-Act Non-Atomic Stock Decrement | **CORRECT** | *Pending* | - | - | - | - |
| `inc_06_connection_exhaustion` | DB Connection Pool | Unclosed Connection on ValueError | **CORRECT** | *Pending* | - | - | - | - |
| `inc_07_retry_storm` | Network / Resiliency | Zero-Backoff Immediate 10x Retry Storm | **CORRECT** | *Pending* | - | - | - | - |
| `inc_08_cascading_timeout` | Cascading Failure | 60s Timeout with Disabled Circuit Breaker | **CORRECT** | *Pending* | - | - | - | - |
| `inc_09_dropped_index` | Database / Indexing | Dropped Compound Index `idx_tenant_status_name` | **CORRECT** | *Pending* | - | - | - | - |
| `inc_10_multi_symptom_cascade` | Complex Multi-Symptom | Dropped Composite Index `idx_ledger_account_entry_date` | **CORRECT** | *Pending* | - | - | - | - |

### Baseline Summary Metrics
- **Model Used**: `openai/gpt-oss-120b` (Groq API, free tier)
- **Total Incidents**: 10
- **Evaluated**: 10
- **Baseline Accuracy**: 10/10 (100% on root cause guess)
- **Verification Score**: 0% (Baseline performs zero executable verification)
- **Average Latency**: 15.98s
- **Total Input Tokens**: 19,877
- **Total Output Tokens**: 6,667

