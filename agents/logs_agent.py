"""Logs Agent: deterministic log extraction plus one Groq evidence-summarisation call.

Responsibility: answer "What useful evidence does the application log contain?"
This agent does not diagnose the incident root cause.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

import jsonschema

from agents.log_tools import collect_candidate_evidence, load_log_lines
from core.llm import (
    GroqLLMClient,
    LLMConfigurationError,
    LLMError,
    LLMJSONParseError,
    get_llm_client,
)


SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "logs_agent_schema.json"
RELATIVE_LOG_PATH = "logs/application.log"


def load_logs_agent_schema() -> Dict[str, Any]:
    """Load the dedicated Logs Agent JSON schema."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class LogsAgent:
    """Specialized evidence agent for application logs."""

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self.schema = load_logs_agent_schema()

    def extract_evidence(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Extract structured log evidence from an incident bundle.

        Makes at most one Groq summarisation call after deterministic extraction.
        Never reads ground_truth.md or other evaluation labels.
        """
        incident_path = Path(incident_dir)
        if not incident_path.exists() or not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")

        incident_id = incident_path.name
        log_file = incident_path / RELATIVE_LOG_PATH
        if not log_file.exists():
            return self._empty_result(
                incident_id,
                summary="No application log file found at logs/application.log.",
            )

        raw_text = log_file.read_text(encoding="utf-8", errors="ignore")
        lines = load_log_lines(raw_text)
        candidates = collect_candidate_evidence(lines, relative_path=RELATIVE_LOG_PATH)

        if not candidates:
            return self._empty_result(
                incident_id,
                summary="Deterministic log analysis found no useful error, warning, burst, or identifier evidence.",
            )

        prompt = self._build_prompt(incident_id, candidates)
        system_prompt = (
            "You are a production log evidence specialist. "
            "You organise and prioritise already-extracted log evidence. "
            "You do not diagnose the incident root cause. "
            "You do not invent log lines, line numbers, timestamps, or references. "
            "You must respond ONLY with a valid JSON object matching the requested schema."
        )

        try:
            response = self.llm_client.generate_structured(
                prompt=prompt,
                schema=self.schema,
                system_prompt=system_prompt,
                temperature=0.0,
            )
            parsed = response.get_structured()
        except LLMJSONParseError:
            return self._fallback_from_candidates(
                incident_id,
                candidates,
                summary="LLM returned malformed JSON; using deterministic log evidence only.",
            )

        return self._normalize_and_validate(parsed, incident_id, candidates)

    def _build_prompt(self, incident_id: str, candidates: List[Dict[str, Any]]) -> str:
        compact = []
        for item in candidates:
            compact.append(
                {
                    "reference": item["reference"],
                    "timestamp": item.get("timestamp", ""),
                    "type": item["type"],
                    "excerpt": item["excerpt"],
                }
            )
        pattern_counts = candidates[0].get("pattern_counts", {}) if candidates else {}

        return f"""Incident ID: {incident_id}

You are given candidate log evidence already extracted by deterministic tools.
Each excerpt is an exact original log line. Each reference is a real file:line locator.

Do NOT diagnose the root cause.
Do NOT invent new log lines or line numbers.
Do NOT read or request ground truth.

Your job:
1. Organise and prioritise the useful observations.
2. Write a short observational summary (symptoms, bursts, retries, latency, pool pressure, etc.).
3. Return the evidence items with interpretations that help a later agent, without claiming verified causality.

Candidate evidence:
{json.dumps(compact, indent=2)}

Deterministic pattern counts (match-per-line):
{json.dumps(pattern_counts, indent=2)}

Return JSON only:
{{
  "incident_id": "{incident_id}",
  "agent": "logs_agent",
  "summary": "<observational summary, not a root-cause claim>",
  "evidence": [
    {{
      "evidence_id": "EV-LOG-001",
      "source": "logs",
      "reference": "logs/application.log:<real line number from candidates>",
      "timestamp": "<timestamp from the candidate or empty string>",
      "type": "error|warning|pattern|burst|context",
      "excerpt": "<exact candidate excerpt>",
      "interpretation": "<what this line shows, observational only>"
    }}
  ]
}}
"""

    def _normalize_and_validate(
        self,
        data: Any,
        expected_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}

        candidate_by_ref = {c["reference"]: c for c in candidates}
        candidate_by_excerpt = {c["excerpt"]: c for c in candidates}

        raw_items = data.get("evidence")
        if not isinstance(raw_items, list):
            raw_items = []

        cleaned: List[Dict[str, Any]] = []
        seen_refs: set[str] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("excerpt", "")).rstrip("\n")
            reference = str(item.get("reference", ""))
            matched = candidate_by_ref.get(reference) or candidate_by_excerpt.get(excerpt)
            if matched is None:
                continue
            ref = matched["reference"]
            if ref in seen_refs:
                continue
            seen_refs.add(ref)
            cleaned.append(
                {
                    "evidence_id": f"EV-LOG-{len(cleaned) + 1:03d}",
                    "source": "logs",
                    "reference": ref,
                    "timestamp": str(item.get("timestamp") or matched.get("timestamp") or ""),
                    "type": matched["type"],
                    "excerpt": matched["excerpt"],
                    "interpretation": str(item.get("interpretation") or "").strip(),
                }
            )

        if not cleaned:
            return self._fallback_from_candidates(
                expected_id,
                candidates,
                summary=str(data.get("summary") or "Using deterministic log evidence; LLM items could not be grounded to real lines."),
            )

        result = {
            "incident_id": expected_id,
            "agent": "logs_agent",
            "summary": str(data.get("summary") or "Organised log evidence from deterministic extraction.").strip(),
            "evidence": cleaned,
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result

    def _fallback_from_candidates(
        self,
        incident_id: str,
        candidates: List[Dict[str, Any]],
        summary: str,
    ) -> Dict[str, Any]:
        evidence = []
        seen: set[str] = set()
        for item in candidates:
            ref = item["reference"]
            if ref in seen:
                continue
            seen.add(ref)
            evidence.append(
                {
                    "evidence_id": f"EV-LOG-{len(evidence) + 1:03d}",
                    "source": "logs",
                    "reference": ref,
                    "timestamp": item.get("timestamp") or "",
                    "type": item["type"],
                    "excerpt": item["excerpt"],
                    "interpretation": "Deterministic extraction; LLM interpretation unavailable.",
                }
            )
        result = {
            "incident_id": incident_id,
            "agent": "logs_agent",
            "summary": summary,
            "evidence": evidence,
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result

    def _empty_result(self, incident_id: str, summary: str) -> Dict[str, Any]:
        result = {
            "incident_id": incident_id,
            "agent": "logs_agent",
            "summary": summary,
            "evidence": [],
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Logs Evidence Agent")
    parser.add_argument(
        "incident_dir",
        type=str,
        help="Path to incident bundle directory (e.g., incidents/inc_01_n_plus_one_query)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path to save JSON evidence output",
    )
    args = parser.parse_args()

    try:
        agent = LogsAgent()
        print(f"[LogsAgent] Extracting log evidence from: {args.incident_dir}")
        result = agent.extract_evidence(args.incident_dir)
        formatted = json.dumps(result, indent=2)
        print("\n=== LOGS AGENT EVIDENCE ===")
        print(formatted)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(formatted, encoding="utf-8")
            print(f"\n[LogsAgent] Saved result to: {args.output}")
    except LLMConfigurationError as e:
        print(f"\n[Configuration Error] {e}")
        print("Tip: Add GROQ_API_KEY=your_key to your .env file or environment.")
        raise SystemExit(1)
    except LLMError as e:
        print(f"\n[LLM Error] Logs Agent failed: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
