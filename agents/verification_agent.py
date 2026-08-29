"""Verification Agent — tests hypotheses via deterministic read-only checks.

Zero LLM calls. The verdict for each hypothesis is computed strictly from the
check results produced by verification_tools dispatch.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Set

import jsonschema

from agents.verification_tools import (
    CheckResult,
    VERDICT_CONFIRMED,
    VERDICT_INCONCLUSIVE,
    VERDICT_REJECTED,
    CHECK_FAIL,
    CHECK_INCONCLUSIVE,
    CHECK_PASS,
    run_dispatch_check,
)

VERIFICATION_SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "verification_schema.json"


def _load_verification_schema() -> Dict[str, Any]:
    with open(VERIFICATION_SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


VERIFICATION_OUTPUT_SCHEMA = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "type": "object",
    "required": ["incident_id", "results"],
    "properties": {
        "incident_id": {"type": "string"},
        "results": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["hypothesis_id", "verdict", "checks", "reasoning", "confidence"],
                "properties": {
                    "hypothesis_id": {"type": "string", "pattern": "^HYP-[0-9]{3}$"},
                    "verdict": {"type": "string", "enum": [VERDICT_CONFIRMED, VERDICT_REJECTED, VERDICT_INCONCLUSIVE]},
                    "checks": {
                        "type": "array",
                        "minItems": 1,
                        "items": {
                            "type": "object",
                            "required": ["check_id", "description", "result", "evidence", "reference"],
                            "properties": {
                                "check_id": {"type": "string", "pattern": "^CHK-[0-9]{3}$"},
                                "description": {"type": "string"},
                                "result": {"type": "string", "enum": [CHECK_PASS, CHECK_FAIL, CHECK_INCONCLUSIVE]},
                                "evidence": {"type": "array", "items": {"type": "string"}},
                                "reference": {"type": ["string", "null"]},
                                "detail": {"type": ["string", "null"]},
                            },
                            "additionalProperties": True,
                        },
                    },
                    "reasoning": {"type": "string"},
                    "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
                },
                "additionalProperties": False,
            },
        },
    },
    "additionalProperties": False,
}


def _flatten_evidence(
    logs: Optional[Dict[str, Any]],
    metrics: Optional[Dict[str, Any]],
    code: Optional[Dict[str, Any]],
) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for bundle in (logs, metrics, code):
        if not isinstance(bundle, dict):
            continue
        for ev in bundle.get("evidence") or []:
            if isinstance(ev, dict) and isinstance(ev.get("evidence_id"), str):
                out[ev["evidence_id"]] = ev
    return out


def _validate_evidence_ids_presence(evidence_ids: List[str], available: Set[str]) -> List[str]:
    return [eid for eid in evidence_ids if eid in available]


def _checks_to_verdict(checks: List[CheckResult]) -> tuple[str, float, str]:
    """Compute hypothesis verdict from checks.

    - REJECTED if any check FAILs
    - CONFIRMED if all mapped checks PASS (≥1 PASS and 0 FAIL)
    - INCONCLUSIVE otherwise (only INCONCLUSIVE checks, or a mix with no FAIL but also no PASS)
    """
    n_fail = sum(1 for c in checks if c.result == CHECK_FAIL)
    n_pass = sum(1 for c in checks if c.result == CHECK_PASS)
    n_inc = sum(1 for c in checks if c.result == CHECK_INCONCLUSIVE)
    total = n_fail + n_pass + n_inc
    if total == 0:
        return VERDICT_INCONCLUSIVE, 0.0, "No checks could be mapped for this hypothesis."
    if n_fail > 0:
        conf = max(0.5, min(0.95, 0.5 + 0.45 * (n_fail / total)))
        fail_refs = [c.reference for c in checks if c.result == CHECK_FAIL and c.reference]
        return (
            VERDICT_REJECTED,
            round(conf, 2),
            "Deterministic falsification check(s) failed: "
            + "; ".join([c.description for c in checks if c.result == CHECK_FAIL][:2])
            + (f" (at {', '.join(fail_refs[:2])})" if fail_refs else "."),
        )
    if n_pass > 0 and n_fail == 0:
        conf = max(0.5, min(0.95, 0.5 + 0.45 * (n_pass / total)))
        pass_refs = [c.reference for c in checks if c.result == CHECK_PASS and c.reference]
        return (
            VERDICT_CONFIRMED,
            round(conf, 2),
            "All required checks passed: "
            + "; ".join([c.description for c in checks if c.result == CHECK_PASS][:3])
            + (f" (references: {', '.join(pass_refs[:3])})" if pass_refs else "."),
        )
    return (
        VERDICT_INCONCLUSIVE,
        0.25,
        "Verification checks were inconclusive; claim is neither confirmed nor rejected.",
    )


class VerificationAgent:
    """Deterministic verifier — zero LLM calls. Read-only."""

    def __init__(self) -> None:
        self.check_schema = _load_verification_schema()

    def verify(
        self,
        incident_dir: Path | str,
        hypotheses: Dict[str, Any],
        logs_evidence: Optional[Dict[str, Any]] = None,
        metrics_evidence: Optional[Dict[str, Any]] = None,
        code_evidence: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        incident_path = Path(incident_dir)
        if not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")
        incident_id = incident_path.name

        if not isinstance(hypotheses, dict) or not isinstance(hypotheses.get("hypotheses"), list):
            raise ValueError("Hypotheses must be a dict with a hypotheses list.")

        ev_by_id = _flatten_evidence(logs_evidence, metrics_evidence, code_evidence)
        allowed_ev_ids = set(ev_by_id.keys())

        results: List[Dict[str, Any]] = []
        for hyp in hypotheses["hypotheses"]:
            if not isinstance(hyp, dict):
                continue
            hyp_id = hyp.get("hypothesis_id")
            if not isinstance(hyp_id, str):
                continue
            evidence_ids = hyp.get("evidence_ids") or []
            valid_ev = _validate_evidence_ids_presence(evidence_ids, allowed_ev_ids)
            referenced = [ev_by_id[eid] for eid in valid_ev]

            plan_steps = hyp.get("verification_plan") or []
            falsification = hyp.get("falsification_criteria") or []
            all_steps = list(plan_steps) + [f"Falsification: {fc}" for fc in falsification]

            checks_out: List[Dict[str, Any]] = []
            raw_checks: List[CheckResult] = []
            check_counter = 0
            for step in all_steps:
                check_counter += 1
                check_id = f"CHK-{check_counter:03d}"
                raw = run_dispatch_check(
                    check_id=check_id,
                    incident_dir=incident_path,
                    hypothesis_claim=hyp.get("claim", ""),
                    plan_step=step,
                    evidence_bundles={
                        "logs": logs_evidence or {},
                        "metrics": metrics_evidence or {},
                        "code": code_evidence or {},
                    },
                    referenced_evidence=referenced,
                )
                raw_checks.append(raw)
                # also validate against existing verification_schema.json for *each* check
                check_dict = {
                    "check_id": raw.check_id,
                    "description": raw.description,
                    "result": raw.result,
                    "evidence": list(raw.evidence) if raw.evidence else [],
                    "reference": raw.reference,
                    "detail": raw.detail,
                }
                checks_out.append(check_dict)

            verdict, confidence, reasoning = _checks_to_verdict(raw_checks)
            results.append(
                {
                    "hypothesis_id": hyp_id,
                    "verdict": verdict,
                    "checks": checks_out,
                    "reasoning": reasoning,
                    "confidence": confidence,
                }
            )

        if not results:
            raise ValueError("No valid hypotheses to verify.")

        output = {"incident_id": incident_id, "results": results}
        jsonschema.validate(instance=output, schema=VERIFICATION_OUTPUT_SCHEMA)
        return output


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Verification Agent CLI (zero LLM)")
    parser.add_argument("incident_dir", type=Path)
    parser.add_argument("--hypotheses", type=Path, required=True)
    parser.add_argument("--logs", type=Path)
    parser.add_argument("--metrics", type=Path)
    parser.add_argument("--code", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    agent = VerificationAgent()
    hypotheses = _load_json(args.hypotheses)
    logs = _load_json(args.logs) if args.logs and args.logs.exists() else None
    metrics = _load_json(args.metrics) if args.metrics and args.metrics.exists() else None
    code = _load_json(args.code) if args.code and args.code.exists() else None
    result = agent.verify(
        incident_dir=args.incident_dir,
        hypotheses=hypotheses,
        logs_evidence=logs,
        metrics_evidence=metrics,
        code_evidence=code,
    )
    text = json.dumps(result, indent=2)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text, encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    _cli()
