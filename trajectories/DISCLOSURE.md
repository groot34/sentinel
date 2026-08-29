# Agent Usage Disclosure

In compliance with the hackathon transparency requirements, this document records all coding agents, models, and AI tools used during the development of Sentinel.

## Coding Agent & Model Registry

| Phase / Task | Agent / Assistant | Underlying Model | Tooling / Harness | Date / Version |
|:---|:---|:---|:---|:---|
| *Repository Skeleton & Setup* | Antigravity AI Coding Agent | Gemini 3.7 Flash | Antigravity IDE | 2026-08-28 |
| *Synthetic Incidents Generation* | *[To be recorded upon execution]* | *[Model]* | *[Harness]* | *[Date]* |
| *Baseline Implementation* | Antigravity AI Coding Agent | Gemini 3.7 Flash | Antigravity IDE | 2026-08-29 |
| *Baseline Evaluation Benchmark* | Antigravity AI Coding Agent | Claude Sonnet 4.6 | Antigravity IDE | 2026-08-29 |
| *Logs Agent + Deterministic Log Tools* | Cursor Grok 4.6 | Cursor Grok 4.6 | Cursor IDE | 2026-08-29 |
| *Advanced Sentinel Multi-Agent* | *[To be recorded upon execution]* | *[Model]* | *[Harness]* | *[Date]* |
| *Evaluation & Benchmark Execution* | *[To be recorded upon execution]* | *[Model]* | *[Harness]* | *[Date]* |

## Development Principles & Disclosure Rules
1. **Full Traceability**: All agent sessions, prompt trajectories, tool calls, and human-in-the-loop checkpoints during development must be documented in `trajectories/`.
2. **Runtime Separation**: The coding agent building this project is distinct from Sentinel's runtime agents (Logs Agent, Metrics Agent, Code Agent, Hypothesis Engine, Verification Agent, Fix Proposal Agent).
3. **No Synthetic Result Fabrication**: All evaluation scores in benchmark documents must reflect genuine execution outputs.
