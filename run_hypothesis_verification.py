"""Run the full Sentinel reasoning chain for specified incidents.

Uses real Groq only for: agent evidence summaries + hypothesis generation.
Verification is deterministic (zero Groq).
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

REPO = Path(__file__).parent
INCIDENTS = REPO / "incidents"
SAMPLE_RUNS = REPO / "eval" / "sample_runs"


def _run_logs_agent(inc_dir: Path) -> Dict[str, Any]:
    from agents.logs_agent import LogsAgent
    agent = LogsAgent()
    return agent.extract_evidence(inc_dir)


def _run_metrics_agent(inc_dir: Path) -> Dict[str, Any]:
    from agents.metrics_agent import MetricsAgent
    agent = MetricsAgent()
    return agent.extract_evidence(inc_dir)


def _run_code_agent(inc_dir: Path) -> Dict[str, Any]:
    from agents.code_agent import CodeAgent
    agent = CodeAgent()
    return agent.extract_evidence(inc_dir)


def _ensure_sample(kind: str, inc_suffix: str, inc_dir: Path) -> Dict[str, Any]:
    sample = SAMPLE_RUNS / f"{kind}_agent_inc_{inc_suffix}.json"
    if sample.exists():
        print(f"  [reuse] {sample.name}")
        return json.loads(sample.read_text(encoding="utf-8"))
    print(f"  [run  ] {kind}_agent for inc_{inc_suffix}...")
    if kind == "logs":
        result = _run_logs_agent(inc_dir)
    elif kind == "metrics":
        result = _run_metrics_agent(inc_dir)
    elif kind == "code":
        result = _run_code_agent(inc_dir)
    else:
        raise ValueError(kind)
    SAMPLE_RUNS.mkdir(parents=True, exist_ok=True)
    sample.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(f"  [save ] -> {sample.name}")
    return result


def _run_hypothesis(inc_id: str, logs: Dict, metrics: Dict, code: Dict) -> Dict[str, Any]:
    from agents.hypothesis_engine import HypothesisEngine
    engine = HypothesisEngine()
    return engine.generate_hypotheses(inc_id, logs, metrics, code)


def _run_verification(inc_dir: Path, hyps: Dict, logs: Dict, metrics: Dict, code: Dict) -> Dict[str, Any]:
    from agents.verification_agent import VerificationAgent
    agent = VerificationAgent()
    return agent.verify(inc_dir, hyps, logs, metrics, code)


def process_incident(inc_name: str, inc_suffix: str) -> Dict[str, Any]:
    inc_id = inc_name
    inc_dir = INCIDENTS / inc_name
    print(f"\n=== Processing {inc_name} ===")
    logs = _ensure_sample("logs", inc_suffix, inc_dir)
    metrics = _ensure_sample("metrics", inc_suffix, inc_dir)
    code = _ensure_sample("code", inc_suffix, inc_dir)
    print(f"  [run  ] hypothesis_engine...")
    hyps = _run_hypothesis(inc_id, logs, metrics, code)
    hyp_out = SAMPLE_RUNS / f"hypothesis_inc_{inc_suffix}.json"
    hyp_out.write_text(json.dumps(hyps, indent=2), encoding="utf-8")
    print(f"  [save ] -> {hyp_out.name} ({len(hyps['hypotheses'])} hypotheses)")
    print(f"  [run  ] verification_agent (zero Groq)...")
    ver = _run_verification(inc_dir, hyps, logs, metrics, code)
    combined = {
        "incident_id": inc_id,
        "logs_evidence": logs,
        "metrics_evidence": metrics,
        "code_evidence": code,
        "hypotheses": hyps,
        "verification": ver,
    }
    out = SAMPLE_RUNS / f"hypothesis_verification_inc_{inc_suffix}.json"
    out.write_text(json.dumps(combined, indent=2), encoding="utf-8")
    print(f"  [save ] -> {out.name}")
    for r in ver["results"]:
        print(f"    {r['hypothesis_id']}: {r['verdict']} (conf={r['confidence']}) checks={len(r['checks'])}")
    return combined


def main() -> None:
    targets = [
        ("inc_01_n_plus_one_query", "01"),
        ("inc_04_memory_leak", "04"),
        ("inc_07_retry_storm", "07"),
        ("inc_10_multi_symptom_cascade", "10"),
    ]
    results = {}
    for inc_name, inc_suffix in targets:
        results[inc_suffix] = process_incident(inc_name, inc_suffix)
    print("\n=== SUMMARY ===")
    for suffix, comb in results.items():
        print(f"inc_{suffix}:")
        for r in comb["verification"]["results"]:
            print(f"  {r['hypothesis_id']}: {r['verdict']:12s} conf={r['confidence']:.2f}  claim={r.get('reasoning','')[:80]}")


if __name__ == "__main__":
    main()
