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

## 2. Benchmark Summary Table Template

| Incident ID | Incident Category | Baseline Diagnosis | Baseline Accuracy | Sentinel Diagnosis | Sentinel Status | Sentinel Evidence Count | Sentinel Accuracy | Verification Pass |
|:---|:---|:---|:---|:---|:---|:---|:---|:---|
| INC-001 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-002 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-003 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-004 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-005 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-006 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-007 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-008 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-009 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |
| INC-010 | *TBD* | *Pending* | - | *Pending* | - | - | - | - |

*(Note: Official evaluation results will be populated upon execution of synthetic test bundles).*
