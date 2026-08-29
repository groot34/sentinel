"""Code Agent: deterministic code/diff extraction plus one Groq evidence-summarisation call.

Responsibility: answer "What changed in the code, and what technically significant behaviour
does that change introduce?" This agent does not diagnose the incident root cause.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import jsonschema

from agents.code_tools import collect_candidate_evidence
from core.llm import (
    GroqLLMClient,
    LLMConfigurationError,
    LLMError,
    LLMJSONParseError,
    get_llm_client,
)


SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "code_agent_schema.json"


def load_code_agent_schema() -> Dict[str, Any]:
    """Load the dedicated Code Agent JSON schema."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class CodeAgent:
    """Specialized evidence agent for source code and git diffs."""

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self.schema = load_code_agent_schema()

    def extract_evidence(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Extract structured code evidence from an incident bundle.

        Makes at most one Groq summarisation call after deterministic extraction.
        Never reads ground_truth.md or other evaluation labels.
        """
        incident_path = Path(incident_dir)
        if not incident_path.exists() or not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")

        incident_id = incident_path.name
        has_diff = (incident_path / "git_diff.patch").exists()
        has_source = (incident_path / "service").exists()
        if not has_diff and not has_source:
            return self._empty_result(
                incident_id,
                summary="No git_diff.patch or service/ source directory found.",
            )

        candidates = collect_candidate_evidence(incident_path)
        if not candidates:
            return self._empty_result(
                incident_id,
                summary="Deterministic code analysis found no added, removed, or suspicious code patterns.",
            )

        prompt = self._build_prompt(incident_id, candidates)
        system_prompt = (
            "You are a production code-change evidence specialist. "
            "You organise and prioritise already-extracted source and git-diff evidence. "
            "You do not diagnose the incident root cause. "
            "You do not invent files, line numbers, hunks, or source text. "
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
                summary="LLM returned malformed JSON; using deterministic code evidence only.",
            )

        return self._normalize_and_validate(parsed, incident_id, candidates)

    def _build_prompt(self, incident_id: str, candidates: List[Dict[str, Any]]) -> str:
        compact = []
        for item in candidates:
            compact.append(
                {
                    "source": item["source"],
                    "reference": item["reference"],
                    "type": item["type"],
                    "excerpt": item["excerpt"],
                    "metadata": item.get("metadata") or {},
                }
            )
        return f"""Incident ID: {incident_id}

You are given candidate code evidence already extracted by deterministic tools.
Each excerpt is exact original source or diff text. Each reference points to a real file, line range, or patch hunk.

Do NOT diagnose the root cause.
Do NOT invent files, line numbers, or code.
Do NOT read or request ground truth.

Your job:
1. Organise and prioritise useful observations about what changed and what behaviour the change may introduce.
2. Write a short observational summary.
3. Return evidence items with interpretations that help a later agent, without claiming verified causality.

Candidate evidence:
{json.dumps(compact, indent=2)}

Return JSON only:
{{
  "incident_id": "{incident_id}",
  "agent": "code_agent",
  "summary": "<observational summary, not a root-cause claim>",
  "evidence": [
    {{
      "evidence_id": "EV-CODE-001",
      "source": "git_diff|code|config",
      "reference": "<real reference from candidates>",
      "type": "added_code|removed_code|suspicious_pattern|changed_config",
      "excerpt": "<exact candidate excerpt>",
      "interpretation": "<what this change or pattern shows, observational only>"
    }}
  ]
}}
"""

    def _candidate_key(self, item: Dict[str, Any]) -> Tuple[str, str, str]:
        return (str(item.get("reference", "")), str(item.get("type", "")), str(item.get("excerpt", "")).strip())

    def _normalize_and_validate(
        self,
        data: Any,
        expected_id: str,
        candidates: List[Dict[str, Any]],
    ) -> Dict[str, Any]:
        if not isinstance(data, dict):
            data = {}

        by_key = {self._candidate_key(c): c for c in candidates}
        by_ref = {}
        for c in candidates:
            by_ref.setdefault(c["reference"], c)

        raw_items = data.get("evidence")
        if not isinstance(raw_items, list):
            raw_items = []

        cleaned: List[Dict[str, Any]] = []
        seen: set[Tuple[str, str, str]] = set()
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            excerpt = str(item.get("excerpt", "")).rstrip("\n")
            reference = str(item.get("reference", ""))
            item_type = str(item.get("type", ""))
            matched = by_key.get((reference, item_type, excerpt.strip())) or by_ref.get(reference)
            if matched is None:
                continue
            key = self._candidate_key(matched)
            if key in seen:
                continue
            seen.add(key)
            cleaned.append(
                {
                    "evidence_id": f"EV-CODE-{len(cleaned) + 1:03d}",
                    "source": matched["source"],
                    "reference": matched["reference"],
                    "type": matched["type"],
                    "excerpt": matched["excerpt"],
                    "interpretation": str(item.get("interpretation") or "").strip(),
                }
            )

        if not cleaned:
            return self._fallback_from_candidates(
                expected_id,
                candidates,
                summary=str(
                    data.get("summary")
                    or "Using deterministic code evidence; LLM items could not be grounded to real source/diff."
                ),
            )

        result = {
            "incident_id": expected_id,
            "agent": "code_agent",
            "summary": str(data.get("summary") or "Organised code evidence from deterministic extraction.").strip(),
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
                    "evidence_id": f"EV-CODE-{len(evidence) + 1:03d}",
                    "source": item["source"],
                    "reference": item["reference"],
                    "type": item["type"],
                    "excerpt": item["excerpt"],
                    "interpretation": "Deterministic extraction; LLM interpretation unavailable.",
                }
            )
        result = {
            "incident_id": incident_id,
            "agent": "code_agent",
            "summary": summary,
            "evidence": evidence,
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result

    def _empty_result(self, incident_id: str, summary: str) -> Dict[str, Any]:
        result = {
            "incident_id": incident_id,
            "agent": "code_agent",
            "summary": summary,
            "evidence": [],
        }
        jsonschema.validate(instance=result, schema=self.schema)
        return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Code Evidence Agent")
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
        agent = CodeAgent()
        print(f"[CodeAgent] Extracting code evidence from: {args.incident_dir}")
        result = agent.extract_evidence(args.incident_dir)
        formatted = json.dumps(result, indent=2)
        print("\n=== CODE AGENT EVIDENCE ===")
        print(formatted)
        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(formatted, encoding="utf-8")
            print(f"\n[CodeAgent] Saved result to: {args.output}")
    except LLMConfigurationError as e:
        print(f"\n[Configuration Error] {e}")
        print("Tip: Add GROQ_API_KEY=your_key to your .env file or environment.")
        raise SystemExit(1)
    except LLMError as e:
        print(f"\n[LLM Error] Code Agent failed: {e}")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
