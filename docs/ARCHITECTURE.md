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
│ Free Groq API (`llama-3.3-70b-versatile`)                   │
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
agents/metrics_agent.py ─────┼──► core/llm.py (GroqLLMClient) ──► Groq API (llama-3.3-70b-versatile)
agents/code_agent.py ────────┤
agents/verification_agent.py ┘
```

### Key Architectural Guardrails
1. **Zero Secret Leakage**: `_sanitize_message` strips Groq/OpenAI pattern tokens from all exceptions, error logs, and stack traces.
2. **Rate Limit & Backoff**: Exponential backoff on 429 rate limit errors with conservative retry limits (default 2 retries) to preserve free-tier quotas.
3. **Structured JSON Output**: Native JSON mode with markdown wrapper stripping ensures deterministic contract conformance with `schemas/*.json`.
4. **Deterministic Work Before LLM**: Agents use deterministic Python tools for log pattern parsing, metric spike detection, and git diff analysis before calling Groq.
