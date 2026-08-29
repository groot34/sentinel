# Sentinel — Solution Video Script

## Video Overview

- **Target Duration**: 4:30 – 5:00 minutes
- **Narration word count**: ~730 words (at 140–150 WPM ≈ 4:52–5:13 min)
- **Core Message**: *A plausible LLM answer is not a verified diagnosis.*
- **Language**: British English
- **Live walkthrough**: Incident 10 — `inc_10_multi_symptom_cascade`

> **Screen directions are in block-quotes. They are not spoken and are not counted in the word total.**

---

## Act 1 — The Problem: Plausible Is Not Verified (0:00 – 0:40)

> **Screen**: Terminal scrolling `incidents/inc_10_multi_symptom_cascade/logs/application.log` alongside a metrics chart — query latency climbing from ~3 ms to 10,000 ms, error rate reaching 100 %.

**Narration** (~100 words):

"It is 14:04 on a Thursday. The ledger service starts throwing 504s. Query latency jumps from three milliseconds to ten seconds, the database connection pool pins at fifteen out of fifteen, readiness probes start failing, and within three minutes the ingress is returning 502s to every client.

The obvious move is to paste this into an LLM and ask for a root cause. You will get a confident answer in seconds. But that answer is plausible — not verified. The model cannot tell you whether it has found the cause or a downstream symptom. In production, those are not the same thing. Sentinel was built to close that gap."

---

## Act 2 — The Architecture (0:40 – 1:20)

> **Screen**: Static architecture diagram from README.md §6 — highlight each stage as it is named.

**Narration** (~105 words):

"Sentinel runs an eight-stage pipeline. Three LLM-assisted agents — Logs, Metrics, and Code — inspect their data sources independently and emit labelled evidence items with exact file and line references. The Orchestrator, which makes zero LLM calls, fuses that evidence and hands it to the Hypothesis Engine, which generates up to four competing, falsifiable hypotheses.

Then comes the critical stage: verification. The Verification Agent is pure Python — AST analysis, log scanning, metric invariant checks. No LLM calls. It either confirms or rejects each hypothesis against the actual incident files.

Only confirmed hypotheses reach the Fix Proposal Agent. Every proposal is held at a human approval gate. Nothing is applied automatically."

---

## Act 3 — Live Walkthrough: Incident 10 (1:20 – 3:20)

> **Screen 3a**: `logs/application.log` and `metrics/metrics.csv` side by side — highlight key lines as named.

**Narration — Evidence** (~140 words):

"Incident 10 is a multi-symptom cascade. The Logs Agent extracts nine evidence items. The earliest — EV-LOG-008 at 14:04:15 — is a transient disk pressure warning. A naive system might stop there. Sentinel labels it a distractor and continues.

At 14:04:45, EV-LOG-005 shows the query on `ledger_entries` taking 1,850 milliseconds — a sequential scan across five million rows. By 14:05:30, EV-LOG-001: the connection pool is fully saturated, 15 out of 15 held by those long-running scans. At 14:06:00, EV-LOG-002: the health endpoint cannot acquire a connection. At 14:06:30, EV-LOG-003: Kubernetes kills the pod after three consecutive probe failures. At 14:07:00, EV-LOG-004: 502s from the ingress.

The Metrics Agent confirms it independently: active DB connections at 15 against a baseline of 2.3, query duration at 10,000 milliseconds, error rate at 100 %. Readiness failures and error rate carry a Pearson correlation of 0.997. The Code Agent finds the dropped index — `idx_ledger_account_entry_date` — in the git diff, and flags that `acquire()` in `app.py` has no guaranteed release path."

---

> **Screen 3b**: `eval/sample_runs/hypothesis_inc_10.json` — scroll through the four hypothesis claims.

**Narration — Hypotheses** (~70 words):

"The Hypothesis Engine produces four competing claims. HYP-001: pool saturation caused by sequential scans. HYP-002: the removed index forced those scans. HYP-003: the connection pool itself leaks — acquire increments without a guaranteed release. HYP-004: the readiness probe failures were a downstream symptom, not a root cause.

Four plausible explanations. Sentinel must verify each one."

---

> **Screen 3c**: `eval/sample_runs/hypothesis_verification_inc_10.json` — verdict fields for each hypothesis.

**Narration — Verification and disclosure** (~110 words):

"The Verification Agent runs deterministic checks — zero further LLM calls. All four hypotheses are CONFIRMED.

HYP-001 and HYP-002 are confirmed by grounding all referenced log lines and metric rows to their exact file positions. HYP-003 is confirmed by an AST check: `get_balance` in `app.py` calls `acquire()` at line 28 with no guaranteed release in the normal execution path. HYP-004 is confirmed by temporal alignment and the 0.997 correlation between probe failures and error rate.

This is a true causal chain: the dropped index forced sequential scans; the scans saturated the pool; the saturated pool caused the health check to fail; the probe failure killed the pod; the dead pod produced the 502s.

One important disclosure: in the final automated benchmark run, Incident 10 hit the Groq free-tier daily token limit during the fix-proposal stage. No fix proposals were produced in that run. The diagnosis and verification are valid. The pipeline status for Incident 10 in the final benchmark is PARTIAL, and this is recorded honestly in `eval/final_summary.json`."

---

## Act 4 — Comparative Evaluation (3:20 – 4:10)

> **Screen**: README.md §7 evaluation table — "Key result at a glance" block, then the full dimension table.

**Narration** (~130 words):

"We benchmarked Sentinel against a single-shot baseline across all ten incidents. Same model — `openai/gpt-oss-120b` at temperature zero. Same evidence. Same incidents.

On raw diagnostic accuracy, both systems score ten out of ten. Sentinel did not improve accuracy.

The difference is verification. The baseline produces zero verification checks. Every answer is an LLM guess with no traceable evidence chain. Sentinel verified ten out of ten — zero per cent to one hundred per cent.

Across ten incidents: 31 hypotheses generated, 30 confirmed, 1 rejected. Every confirmed result has a traceable chain from hypothesis ID to evidence IDs to check IDs to file-and-line reference.

The cost: 58 LLM calls versus 10 for the baseline, and 145 seconds average latency versus 16. Verification itself — the stage that actually proves the diagnosis — uses zero LLM calls."

---

## Act 5 — Conclusion (4:10 – 4:55)

> **Screen**: Repository root → `README.md` Quickstart → `REPRODUCE.md` → terminal running `pytest`.
> **Terminal shown on screen**: `pytest` → `315 passed in 3.2s`

**Narration** (~75 words):

"Sentinel is fully reproducible. Three hundred and fifteen unit tests and 51 incident validation checks pass with zero real API calls. `REPRODUCE.md` explains exactly how to re-run the full benchmark from scratch.

During a pipeline run: zero incident files modified, zero patches applied automatically. The approval gate non-interactively rejects everything unless a human explicitly approves.

The point of Sentinel is simple: in production incident response, plausibility is not a standard of evidence. Verification is."

---

## Production Notes

| Act | Narration words | Est. duration at 145 WPM |
|---|---|---|
| Act 1 — Problem | 100 | 0:41 |
| Act 2 — Architecture | 105 | 0:43 |
| Act 3 — Walkthrough | 320 | 2:13 |
| Act 4 — Evaluation | 130 | 0:54 |
| Act 5 — Conclusion | 75 | 0:31 |
| **Total narration** | **730** | **~5:02** |

*At 140 WPM: ~5:14. At 150 WPM: ~4:52. Mid-range target: 5:00.*

### Files shown on screen (in order)
1. `incidents/inc_10_multi_symptom_cascade/logs/application.log`
2. `incidents/inc_10_multi_symptom_cascade/metrics/metrics.csv`
3. Architecture diagram — `README.md §6`
4. `eval/sample_runs/hypothesis_inc_10.json`
5. `eval/sample_runs/hypothesis_verification_inc_10.json` (verdict fields)
6. README.md §7 evaluation table
7. `REPRODUCE.md`
8. Terminal: `pytest` → 315 passed

### All numerical claims verified against repository artefacts
- Evidence IDs confirmed: `logs_agent_inc_10.json`, `metrics_agent_inc_10.json`, `code_agent_inc_10.json`
- Hypothesis claims confirmed: `hypothesis_inc_10.json`
- All 4 verdicts CONFIRMED: `hypothesis_verification_inc_10.json`
- inc_10 PARTIAL, benchmark fix proposals = 0: `eval/final_summary.json`
- Benchmark numbers (10/10, 10/10, 31/30/1, 58 calls, 15.984 s, 145.436 s): `eval/final_summary.json`
- 315 tests + 51 validation: verified by `pytest` run
