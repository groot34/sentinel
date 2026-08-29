# Sentinel Architecture & Runtime LLM Specification

## 1. Two-Layer AI Model Architecture

To guarantee strict reproducibility and avoid vendor lock-in, Sentinel separates the AI workflow into two distinct layers:

```
┌─────────────────────────────────────────────────────────────┐
│ LAYER A: Coding Assistant (Development Time)                │
│ (Antigravity / Cursor / Claude Code / Local LLM)            │
│ Builds and maintains files in Git repository.               │
└──────────────────────────────┬──────────────────────────────┘
                               │ writes code
                               ▼
┌─────────────────────────────────────────────────────────────┐
│ LAYER B: Sentinel Runtime (Execution Time)                  │
│ Free Groq API (`openai/gpt-oss-120b`)                       │
└──────────────────────────────┬──────────────────────────────┘
                               │
               ┌───────────────┴───────────────┐
               │                               │
               ▼                               ▼
       Baseline System                 Advanced Sentinel
      (Single LLM Guess)         (Multi-Agent Evidence Engine)
```

---

## 2. Centralized Groq Abstraction (`core/llm.py`)

All agents interact exclusively with `core.llm.GroqLLMClient` via `get_llm_client()`. Individual agent modules **never** instantiate third-party SDKs directly.

```
agents/logs_agent.py ────────┐
agents/metrics_agent.py ─────┼──► core/llm.py (GroqLLMClient) ──► Groq API (openai/gpt-oss-120b)
agents/code_agent.py ────────┤
agents/verification_agent.py ┘
```

### Logs Agent pipeline (implemented)

```
incident logs (logs/application.log)
        ↓
deterministic log tools (agents/log_tools.py)
        ↓
candidate evidence (exact excerpts + file:line references)
        ↓
Groq evidence summarisation (one generate_structured call)
        ↓
structured log evidence (schemas/logs_agent_schema.json)
```

The Logs Agent organises observations only. It does not emit a verified root cause.

### Metrics Agent pipeline (implemented)

```
metrics.csv
        ↓
deterministic metric tools (agents/metric_tools.py)
        ↓
candidate quantitative evidence
        ↓
Groq summarisation (one generate_structured call)
        ↓
structured metric evidence (schemas/metrics_agent_schema.json)
```

The Metrics Agent reports what the numbers show (spikes, drops, period shifts, correlations). It does not emit a verified root cause.

### Code Agent pipeline (implemented)

```
source code + git_diff.patch
        ↓
deterministic code tools (agents/code_tools.py)
        ↓
candidate code evidence — added/removed lines, hunks, suspicious patterns
        ↓
small evidence bundle with real file:line or git_diff.patch:hunk references
        ↓
Groq interpretation (one generate_structured call)
        ↓
structured code evidence (schemas/code_agent_schema.json)
```

The Code Agent reports what changed in the source and what behaviour that change may introduce. It does not emit a verified root cause.

### Key Architectural Guardrails
1. **Zero Secret Leakage**: `_sanitize_message` strips Groq/OpenAI pattern tokens from all exceptions, error logs, and stack traces.
2. **Rate Limit & Backoff**: Exponential backoff on 429 rate limit errors with conservative retry limits (default 2 retries) to preserve free-tier quotas.
3. **Structured JSON Output**: Native JSON mode with markdown wrapper stripping ensures deterministic contract conformance with `schemas/*.json`.
4. **Deterministic Work Before LLM**: Agents use deterministic Python tools for log pattern parsing, metric spike detection, and git diff analysis before calling Groq.
