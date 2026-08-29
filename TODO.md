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
- [ ] **Milestone 7: Hypothesis Engine & Verification Agent**
  - Implement `agents/hypothesis_engine.py` generating 1–4 falsifiable hypotheses with evidence IDs.
  - Implement `agents/verification_agent.py` executing programmatic invariant checks (`CONFIRMED`, `REJECTED`, `INCONCLUSIVE`).
- [ ] **Milestone 8: Fix Proposal Agent & Human Approval Gate**
  - Implement `agents/fix_proposal_agent.py` with patch generation and regression test writing.
  - Enforce `"AWAITING HUMAN APPROVAL — this fix has not been applied."`
- [ ] **Milestone 9: Orchestrator Pipeline Integration**
  - Wire end-to-end multi-agent pipeline in `agents/orchestrator.py`.
- [ ] **Milestone 10: Comparative Evaluation & Final Deliverables**
  - Run comparative benchmark: Baseline vs. Sentinel across all 10 incidents.
  - Record quantitative results in `CHANGELOG.md` and `eval/rubric.md`.
  - Finalize `trajectories/`, `video/script.md`, and `REPRODUCE.md`.
