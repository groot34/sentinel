"""Metrics Agent: deterministic metric extraction plus one Groq evidence-summarisation call.

Responsibility: answer "What does the metric data actually show?"
This agent does not diagnose the incident root cause.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from agents.metric_tools import collect_candidate_evidence, load_metrics
from core.llm import (
    GroqLLMClient,
    LLMConfigurationError,
    LLMError,
    LLMJSONParseError,
    get_llm_client,
)


SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "metrics_agent_schema.json"
RELATIVE_METRICS_PATH = "metrics/metrics.csv"


def load_metrics_agent_schema() -> Dict[str, Any]:
    """Load the dedicated Metrics Agent JSON schema."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class MetricsAgent:
    """Specialized evidence agent for quantitative telemetry."""

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self.schema = load_metrics_agent_schema()

    def extract_evidence(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Extract structured metric evidence from an incident bundle.

        Makes at most one Groq summarisation call after deterministic extraction.
        Never reads ground_truth.md or other evaluation labels.
        """
        incident_path = Path(incident_dir)
        if not incident_path.exists() or not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")

        incident_id = incident_path.name
        metrics_file = incident_path / RELATIVE_METRICS_PATH
        if not metrics_file.exists():
            return self._empty_result(
                incident_id,
                summary="No metrics file found at metrics/metrics.csv.",
            )

        try:
            table = load_metrics(metrics_file)
        except ValueError as exc:
            return self._empty_result(incident_id, summary=f"Metrics CSV could not be analysed: {exc}")

        if table.n == 0 or not table.columns:
            return self._empty_result(
                incident_id,
                summary="Metrics CSV contains no numeric samples.",
            )

        candidates = collect_candidate_evidence(table, relative_path=RELATIVE_METRICS_PATH)
        if not candidates:
            return self._empty_result(
                incident_id,
                summary="Deterministic metric analysis found no spikes, drops, threshold violations, or correlations.",
            )

        prompt = self._build_prompt(incident_id, candidates)
        system_prompt = (
            "You are a production metrics evidence specialist. "
            "You organise and prioritise already-extracted quantitative evidence. "
            "You do not diagnose the incident root cause. "
            "You do not invent timestamps, row numbers, metric names, or values. "
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
                summary="LLM returned malformed JSON; using deterministic metric evidence only.",
            )

        return self._normalize_and_validate(parsed, incident_id, candidates)

    def _build_prompt(self, incident_id: str, candidates: List[Dict[str, Any]]) -> str:
        compact = []
        for item in candidates:
            compact.append(
                {
                    "reference": item["reference"],
                    "timestamp": item.get("timestamp", ""),
                    "metric": item["metric"],
                    "value": item["value"],
                    "type": item["type"],
                    "metadata": item.get("metadata") or {},
                }
            )
        return f"""Incident ID: {incident_id}

You are given candidate metric evidence already extracted by deterministic tools.
Each value and timestamp is copied from metrics/metrics.csv. Row numbers are 1-indexed file lines (header is row 1).

Do NOT diagnose the root cause.
Do NOT invent metric names, values, timestamps, or row numbers.
Do NOT read or request ground truth.

Your job:
1. Organise and prioritise useful quantitative observations (spikes, drops, saturation, period shifts, correlations).
2. Write a short observational summary of what the numbers show.
3. Return evidence items with interpretations that help a later agent, without claiming verified causality.

Candidate evidence:
{json.dumps(compact, indent=2)}

Return JSON only:
{{
  "incident_id": "{incident_id}",
  "agent": "metrics_agent",
  "summary": "<observational summary, not a root-cause claim>",
  "evidence": [
    {{
      "evidence_id": "EV-MET-001",
      "source": "metrics",
      "reference": "metrics/metrics.csv:row <real row from candidates>",
      "timestamp": "<timestamp from the candidate>",
      "metric": "<metric name from the candidate>",
      "value": <numeric value from the candidate>,
      "type": "spike|drop|threshold|period_change|correlation|anomaly",
      "interpretation": "<what this number shows, observational only>"
    }}
  ]
}}
"""

    def _candidate_key(self, item: Dict[str, Any]) -> Tuple[str, str, str]:
        return (str(item.get("reference", "")), str(item.get("metric", "")), str(item.get("type", "")))

    def _normalize_and_validate(
        self,
        data: Any,
        expected_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}

        by_key = {self._candidate_key(c): c for c in candidates}
        by_ref_metric = {(c["reference"], c["metric"]): c for c in candidates}

        raw_items = data.get("evidence")
        if not isinstance(raw_items, list):
            raw_items = []

        cleaned: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            reference = str(item.get("reference", ""))
            metric = str(item.get("metric", ""))
            item_type = str(item.get("type", ""))
            matched = by_key.get((reference, metric, item_type)) or by_ref_metric.get((reference, metric))
            if matched is None:
                continue
            key = self._candidate_key(matched)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(
                {
                    "evidence_id": f"EV-MET-{len(cleaned) + 1:03d}",
                    "source": "metrics",
                    "reference": matched["reference"],
                    "timestamp": str(item.get("timestamp") or matched.get("timestamp") or ""),
                    "metric": matched["metric"],
                    "value": matched["value"],
                    "type": matched["type"],
                    "interpretation": str(item.get("interpretation") or "").strip(),
                }
            )

        if not cleaned:
            return self._fallback_from_candidates(
                expected_id,
                candidates,
                summary=str(
                    data.get("summary")
                    or "Using deterministic metric evidence; LLM items could not be grounded to real samples."
                ),
            )

        result = {
            "incident_id": expected_id,
            "agent": "metrics_agent",
            "summary": str(data.get("summary") or "Organised metric evidence from deterministic extraction.").strip(),
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
        seen: set[Tuple[str, str, str]] = set()
        for item in candidates:
            key = self._candidate_key(item)
            if key in seen:
                continue
            seen.add(key)
            evidence.append(
                {
                    "evidence_id": f"EV-MET-{len(evidence) + 1:03d}",
                    "source": "metrics",
                    "reference": item["reference"],
                    "timestamp": item.get("timestamp") or "",
                    "metric": item["metric"],
                    "value": item["value"],
                    "type": item["type"],
                    "interpretation": "Deterministic extraction; LLM interpretation unavailable.",
                }
            )
        result = {
            "incident_id": incident_id,
            "agent": "metrics_agent",
            "summary": summary,
            "evidence": evidence,
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result

    def _empty_result(self, incident_id: str, summary: str) -> Dict[str, Any]:
        result = {
            "incident_id": incident_id,
            "agent": "metrics_agent",
            "summary": summary,
            "evidence": [],
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Metrics Evidence Agent")
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
        agent = MetricsAgent()
        print(f"[MetricsAgent] Extracting metric evidence from: {args.incident_dir}")
        result = agent.extract_evidence(args.incident_dir)
        formatted = json.dumps(result, indent=2)
        print("\n=== METRICS AGENT EVIDENCE ===")
        print(formatted)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(formatted, encoding="utf-8")
            print(f"\n[MetricsAgent] Saved result to: {args.output}")
    except LLMConfigurationError as e:
        print(f"\n[Configuration Error] {e}")
        print("Tip: Add GROQ_API_KEY=your_key to your .env file or environment.")
        raise SystemExit(1)
    except LLMError as e:
        print(f"\n[LLM Error] Metrics Agent failed: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
