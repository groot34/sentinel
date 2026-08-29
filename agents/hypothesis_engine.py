"""Hypothesis Engine — turns evidence from the three specialists into competing
falsifiable claims with exactly one Groq structured-generation call.

The Hypothesis Engine proposes; the Verification Agent tests.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import jsonschema

from core.llm import (
    GroqLLMClient,
    LLMError,
    LLMJSONParseError,
    get_llm_client,
)

SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "hypothesis_schema.json"
HYPOTHESIS_ID_RE = re.compile(r"^HYP-\d{3}$")
EVIDENCE_ID_RE = re.compile(r"^EV-(LOG|MET|CODE)-\d{3}$")


def load_hypothesis_schema() -> Dict[str, Any]:
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _extract_evidence_ids(bundle: Dict[str, Any]) -> List[str]:
    ids: List[str] = []
    if isinstance(bundle, dict):
        items = bundle.get("evidence") or []
        if isinstance(items, list):
            for ev in items:
                if isinstance(ev, dict) and isinstance(ev.get("evidence_id"), str):
                    ids.append(ev["evidence_id"])
    return ids


def _evidence_snippets(bundle: Dict[str, Any]) -> List[str]:
    snippets: List[str] = []
    for ev in (bundle.get("evidence") or []):
        if not isinstance(ev, dict):
            continue
        eid = ev.get("evidence_id")
        src = ev.get("source")
        ref = ev.get("reference")
        typ = ev.get("type")
        exc = (ev.get("excerpt") or "").strip().replace("\n", " ⏎ ")
        interp = (ev.get("interpretation") or "").strip().replace("\n", " ⏎ ")
        snippet = f"- {eid} [{src}/{typ}] ref={ref}: excerpt={exc[:240]}"
        if interp:
            snippet += f"; interpretation={interp[:200]}"
        snippets.append(snippet)
    return snippets


def _validate_output(
    data: Any,
    schema: Dict[str, Any],
    allowed_evidence_ids: Set[str],
    incident_id: str,
) -> Dict[str, Any]:
    """Normalize, validate and (deterministically) repair the hypothesis output.

    Evidence ID integrity: every hypothesis evidence_id must exist in the
    supplied set. Unknown IDs are dropped; if that leaves a hypothesis with no
    evidence the hypothesis is also dropped. Hypothesis IDs are re-stamped
    HYP-001..HYP-NNN deterministically (never trusting LLM IDs).
    """
    if not isinstance(data, dict):
        raise LLMJSONParseError("Hypothesis output must be a JSON object.")

    data["incident_id"] = incident_id
    hypotheses = data.get("hypotheses")
    if not isinstance(hypotheses, list):
        raise LLMJSONParseError("Hypothesis output must contain a list of hypotheses.")

    cleaned: List[Dict[str, Any]] = []
    seen_ids: Set[str] = set()
    for idx, raw in enumerate(hypotheses, start=1):
        if not isinstance(raw, dict):
            continue
        new_id = f"HYP-{idx:03d}"
        if new_id in seen_ids:
            continue
        claim = (raw.get("claim") or "").strip()
        if not claim:
            continue
        ev_ids_raw = raw.get("evidence_ids") or []
        if not isinstance(ev_ids_raw, list):
            ev_ids_raw = []
        valid_ev_ids: List[str] = []
        for eid in ev_ids_raw:
            if isinstance(eid, str) and eid in allowed_evidence_ids and eid not in valid_ev_ids:
                valid_ev_ids.append(eid)
        if not valid_ev_ids:
            continue
        fals = raw.get("falsification_criteria") or []
        fals = [x for x in fals if isinstance(x, str) and x.strip()]
        if not fals:
            fals = ["If no supplied evidence IDs actually support this claim, reject it."]
        plan = raw.get("verification_plan") or []
        plan = [x for x in plan if isinstance(x, str) and x.strip()]
        if not plan:
            plan = ["Cross-check the evidence chain for the supplied IDs."]
        support = (raw.get("supporting_reasoning") or "").strip() or (
            f"Supported by evidence {', '.join(valid_ev_ids)}."
        )
        cleaned.append(
            {
                "hypothesis_id": new_id,
                "claim": claim,
                "evidence_ids": valid_ev_ids,
                "supporting_reasoning": support,
                "falsification_criteria": fals,
                "verification_plan": plan,
            }
        )
        seen_ids.add(new_id)
        if len(cleaned) >= 4:
            break

    if not cleaned:
        raise LLMJSONParseError(
            "LLM produced no evidence-backed hypotheses after validation."
        )

    data["hypotheses"] = cleaned
    jsonschema.validate(instance=data, schema=schema)
    return data


class HypothesisEngine:
    """Generates 1–4 falsifiable hypotheses from evidence bundles."""

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self.schema = load_hypothesis_schema()

    def generate_hypotheses(
        self,
        incident_id: str,
        logs_evidence: Optional[Dict[str, Any]] = None,
        metrics_evidence: Optional[Dict[str, Any]] = None,
        code_evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        bundles = [logs_evidence, metrics_evidence, code_evidence]
        bundles = [b for b in bundles if isinstance(b, dict)]
        ev_ids: List[str] = []
        for b in bundles:
            ev_ids.extend(_extract_evidence_ids(b))
        if not ev_ids:
            raise ValueError("No evidence supplied; cannot generate hypotheses.")
        allowed = set(ev_ids)

        logs_lines = _evidence_snippets(logs_evidence or {})
        metrics_lines = _evidence_snippets(metrics_evidence or {})
        code_lines = _evidence_snippets(code_evidence or {})

        prompt_parts = [
            f"Incident id: {incident_id}",
            "",
            "You will receive evidence collected by three specialised Sentinel agents.",
            "Your task is to propose between 1 and 4 competing, falsifiable hypotheses.",
            "Rules:",
            "1. Every hypothesis evidence_ids entry must be an evidence_id from the supplied lists below only.",
            "2. Never invent evidence IDs or reference IDs that are not listed.",
            "3. State the claim specifically.",
            "4. Falsification criteria must describe a concrete observable state that would disprove the hypothesis.",
            "5. Verification plan steps must be concrete deterministic checks a later agent could execute (no shell commands).",
            "6. Do not state a final diagnosis or single cause — provide competing hypotheses where possible.",
            "",
            f"Known evidence IDs: {', '.join(sorted(allowed))}",
            "",
            "Logs evidence:",
            *(logs_lines or ["(none)"]),
            "",
            "Metrics evidence:",
            *(metrics_lines or ["(none)"]),
            "",
            "Code evidence:",
            *(code_lines or ["(none)"]),
            "",
            "Respond with the requested JSON schema only.",
        ]
        prompt = "\n".join(prompt_parts)

        system_prompt = (
            "You are a Sentinel hypothesis engineer. "
            "You propose competing, falsifiable explanations grounded strictly in the supplied evidence IDs. "
            "You never invent evidence IDs, incident facts, or root-cause diagnoses. "
            "Respond with valid JSON matching the required schema only."
        )

        try:
            resp = self.llm_client.generate_structured(
                prompt=prompt,
                schema=self.schema,
                system_prompt=system_prompt,
                temperature=0.0,
            )
            raw = resp.get_structured()
        except LLMJSONParseError as exc:
            raise LLMJSONParseError(
                f"Hypothesis LLM response could not be parsed: {exc}"
            ) from exc
        except LLMError as exc:
            raise

        return _validate_output(raw, self.schema, allowed, incident_id)


def _load_optional_json(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    if not path.exists():
        return None
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Hypothesis Engine CLI")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    engine = HypothesisEngine()
    result = engine.generate_hypotheses(
        incident_id=args.incident_id,
        logs_evidence=_load_optional_json(args.logs),
        metrics_evidence=_load_optional_json(args.metrics),
        code_evidence=_load_optional_json(args.code),
    )
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    _cli()
