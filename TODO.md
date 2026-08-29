# Sentinel Project Roadmap & Milestone Tracker

## Status Legend
- [x] Completed & Tested
- [ ] In Progress / Upcoming

---

## Completed Milestones
- [x] **Milestone 1: Repository Skeleton & Schema Contracts** (2026-08-28)
- [x] **Milestone 2: Synthetic Incident Dataset (10 Canonical Incidents)** (2026-08-28)
- [x] **Milestone 3: Centralized Groq Runtime LLM Client** (2026-08-29)
- [x] **Milestone 4: Single-Shot Baseline Investigator** (2026-08-29)
- [x] **Milestone 5: Baseline Evaluation Benchmark** (2026-08-29)
  - Implemented deterministic correctness evaluator (`eval/evaluator.py`).
  - Implemented full evaluation runner with resume + rate-limit support (`eval/run_eval.py`).
  - 14 unit tests passing in `tests/test_eval.py`.
  - Executed benchmark on Groq (`openai/gpt-oss-120b`) across all 10 synthetic incidents.
  - Measured 10/10 root-cause accuracy, 0% verification score, 15.98s average latency.
  - Saved outputs to `eval/results_baseline.csv` and `eval/baseline_summary.json`.
- [x] **Milestone 6a: Logs Agent + Deterministic Log Tools** (2026-08-29)
  - Implemented `agents/log_tools.py` (no LLM calls).
  - Implemented `agents/logs_agent.py` using `core.llm` and locked model `openai/gpt-oss-120b`.
  - Schema: `schemas/logs_agent_schema.json`.
  - Unit tests mocked; live Groq sample runs for incidents 01 and 10.
- [x] **Milestone 6b: Metrics Agent + Deterministic Metric Tools** (2026-08-29)
  - Implemented `agents/metric_tools.py` (no LLM calls; baseline z-score spikes/drops).
  - Implemented `agents/metrics_agent.py` using `core.llm` and locked model `openai/gpt-oss-120b`.
  - Schema: `schemas/metrics_agent_schema.json`.
  - Unit tests mocked; live Groq sample runs for incidents 01, 03, and 10.


---

## Upcoming Milestones
- [x] **Milestone 6c: Code Agent**
  - Implemented `agents/code_tools.py` (unified-diff parser, AST/regex pattern detectors, reference-grounded evidence collection).
  - Implemented `agents/code_agent.py` using `core.llm` and locked model `openai/gpt-oss-120b`.
  - Schema: `schemas/code_agent_schema.json`.
  - Unit tests mocked; live Groq sample runs for incidents 01, 04, 07, and 10.
- [x] **Milestone 7: Hypothesis Engine & Verification Agent**
  - Implemented `agents/hypothesis_engine.py` generating 1–4 falsifiable hypotheses with evidence ID validation (one Groq structured call via `core.llm`).
  - Implemented `agents/verification_tools.py`: zero-LLM deterministic AST/log/metric invariant checkers.
  - Implemented `agents/verification_agent.py` executing programmatic invariant checks and emitting `CONFIRMED`, `REJECTED`, `INCONCLUSIVE` verdicts strictly from check results (zero LLM calls).
  - Schema: `schemas/hypothesis_schema.json` (1–4 hypotheses, unique EV-IDs, falsification + plan).
  - Unit tests mocked (46 tests across hypothesis engine + verification tools + verification agent); live Groq + deterministic verification sample runs for incidents 01, 04, 07, and 10.
- [x] **Milestone 8: Fix Proposal Agent & Human Approval Gate**
  - Implemented `agents/fix_proposal_agent.py` — ONE Groq call per CONFIRMED hypothesis; status always PROPOSED; patch never auto-applied.
  - Implemented `agents/fix_tools.py` — zero-LLM deterministic safety validator (10 checks); blocks rejected/inconclusive hypotheses, destructive ops, invalid file refs, and applied-claim assertions.
  - Implemented `agents/approval_gate.py` — explicit state machine PROPOSED → APPROVED | REJECTED; default REJECTED; non-interactive auto-rejects; approval records decision only, no patch application.
  - Schemas: `schemas/fix_proposal_schema.json`, `schemas/approval_schema.json`.
  - Unit tests: 88 new tests (test_fix_tools, test_approval_gate, test_fix_proposal_agent); 261/261 total passing.
  - Live Groq runs for incidents 01, 04, 07, 10; sample outputs saved; source files verified unchanged after every gate run.
- [ ] **Milestone 9: Orchestrator Pipeline Integration**
  - Wire end-to-end multi-agent pipeline in `agents/orchestrator.py`.
- [ ] **Milestone 10: Comparative Evaluation & Final Deliverables**
  - Run comparative benchmark: Baseline vs. Sentinel across all 10 incidents.
  - Record quantitative results in `CHANGELOG.md` and `eval/rubric.md`.
  - Finalize `trajectories/`, `video/script.md`, and `REPRODUCE.md`.
