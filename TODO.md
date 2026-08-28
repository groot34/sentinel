# Sentinel Project Roadmap & Milestone Tracker

## Status Legend
- [x] Completed & Tested
- [ ] In Progress / Upcoming

---

## Completed Milestones
- [x] **Milestone 1: Repository Skeleton & Schema Contracts** (2026-08-28)
  - Initialized project structure, Makefile, `.gitignore`, `.env.example`, `requirements.txt`.
  - Defined strict JSON schemas (`baseline_schema.json`, `evidence_schema.json`, `hypothesis_schema.json`, `verification_schema.json`, `orchestrator_schema.json`, `report_schema.json`).
  - Added schema validation test suite (`tests/test_schemas.py`).
- [x] **Milestone 2: Synthetic Incident Dataset (10 Canonical Incidents)** (2026-08-28)
  - Created 10 realistic, internally consistent incident bundles under `incidents/`.
  - Implemented incident validation test suite (`tests/validate_incidents.py` - 51 checks passing).
- [x] **Milestone 3: Centralized Groq Runtime LLM Client** (2026-08-29)
  - Implemented `core/llm.py` with `GroqLLMClient`, structured JSON generation, rate-limit retries, latency/token telemetry, and secret scrubbing.
  - Added unit test suite (`tests/test_llm.py` - 10 tests passing).
- [x] **Milestone 4: Single-Shot Baseline Investigator** (2026-08-29)
  - Implemented `baseline/baseline_agent.py` using centralized `core.llm`.
  - Added unit test suite (`tests/test_baseline.py` - 7 tests passing).
  - Validated ground truth isolation and schema conformance.
  - Generated sample runs for Incident 01 and Incident 10.

---

## Upcoming Milestones
- [ ] **Milestone 5: Baseline Benchmark Evaluation Runner**
  - Implement full 10-incident evaluation loop in `eval/run_eval.py --mode baseline`.
  - Score baseline accuracy, hallucination rate, and evidence citations against `ground_truth.md`.
- [ ] **Milestone 6: Specialized Evidence Gathering Agents**
  - Implement `agents/logs_agent.py` (deterministic log parser + Groq summarizer).
  - Implement `agents/metrics_agent.py` (statistical anomaly detector + Groq summarizer).
  - Implement `agents/code_agent.py` (diff inspection + AST/file analyzer).
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
