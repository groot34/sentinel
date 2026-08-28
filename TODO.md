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
- [x] **Milestone 5: Baseline Evaluation Harness** (2026-08-29)
  - Deterministic correctness evaluator (`eval/evaluator.py`).
  - Full evaluation runner with resume + rate-limit support (`eval/run_eval.py`).
  - 14 unit tests passing in `tests/test_eval.py`.
  - **PENDING**: Real 10-incident Groq run — requires `GROQ_API_KEY` in `.env`.
    Run: `python -m eval.run_eval --mode baseline --sleep 3`


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
