"""Baseline Agent: Single-Shot LLM Incident Investigator.

This baseline represents a standard, fair single-shot LLM approach to incident diagnosis:
- Ingests all available runtime evidence (logs, metrics, git diffs, source code).
- Explicitly isolates and excludes evaluation files such as `ground_truth.md`.
- Makes exactly ONE primary Groq LLM call via the centralized `core.llm` abstraction.
- Outputs structured JSON conforming strictly to `schemas/baseline_schema.json`.
- Zero specialized tools, zero multi-agent subroutines, and zero verification checks.
"""

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional
import jsonschema

from core.llm import (
    GroqLLMClient,
    get_llm_client,
    LLMJSONParseError,
    LLMError,
    LLMConfigurationError,
)



SCHEMA_PATH = Path(__file__).parent.parent / "schemas" / "baseline_schema.json"


def load_baseline_schema() -> Dict[str, Any]:
    """Load the official JSON schema for baseline output validation."""
    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class BaselineAgent:
    """Fair single-shot baseline investigator using centralized Groq LLM."""

    EXCLUDED_FILES = {
        "ground_truth.md",
        "ground_truth.json",
        ".gitkeep",
        "__pycache__",
    }

    def __init__(self, llm_client: Optional[GroqLLMClient] = None) -> None:
        self.llm_client = llm_client or get_llm_client()
        self.schema = load_baseline_schema()

    def load_incident_evidence(self, incident_dir: Path) -> Dict[str, Any]:
        """Load all available incident telemetry and source code while excluding ground truth.

        Args:
            incident_dir: Path to the incident bundle directory.

        Returns:
            Dictionary with separated evidence categories.
        """
        incident_path = Path(incident_dir)
        if not incident_path.exists() or not incident_path.is_dir():
            raise FileNotFoundError(f"Incident directory not found: {incident_dir}")

        evidence_bundle: Dict[str, Any] = {
            "incident_id": incident_path.name,
            "logs": "",
            "metrics": "",
            "git_diff": "",
            "source_code": {},
            "config": "",
        }

        # 1. Logs
        log_file = incident_path / "logs" / "application.log"
        if log_file.exists():
            evidence_bundle["logs"] = log_file.read_text(encoding="utf-8", errors="ignore")

        # 2. Metrics
        metrics_file = incident_path / "metrics" / "metrics.csv"
        if metrics_file.exists():
            evidence_bundle["metrics"] = metrics_file.read_text(encoding="utf-8", errors="ignore")

        # 3. Git Diff / Recent Changes
        diff_file = incident_path / "git_diff.patch"
        if diff_file.exists():
            evidence_bundle["git_diff"] = diff_file.read_text(encoding="utf-8", errors="ignore")

        # 4. Docker / Infrastructure Config
        docker_file = incident_path / "docker-compose.yml"
        if docker_file.exists():
            evidence_bundle["config"] = docker_file.read_text(encoding="utf-8", errors="ignore")

        # 5. Service Source Code (excluding ground truth and tests)
        service_dir = incident_path / "service"
        if service_dir.exists():
            for src_file in service_dir.rglob("*.py"):
                # Strictly isolate ground truth and test evaluation files
                if any(excluded in src_file.name for excluded in self.EXCLUDED_FILES):
                    continue
                rel_path = str(src_file.relative_to(incident_path))
                evidence_bundle["source_code"][rel_path] = src_file.read_text(
                    encoding="utf-8", errors="ignore"
                )

        return evidence_bundle

    def _build_prompt(self, evidence_bundle: Dict[str, Any]) -> str:
        """Construct a complete, fair prompt containing all incident context."""
        incident_id = evidence_bundle["incident_id"]

        source_code_section = ""
        for filepath, content in evidence_bundle["source_code"].items():
            source_code_section += f"\n--- File: {filepath} ---\n{content}\n"

        prompt = f"""You are an on-call Site Reliability Engineer investigating a production outage for incident '{incident_id}'.

Analyze the provided incident evidence below (logs, metrics, recent code changes, and service source code) and determine the single most likely root cause.

=== RECENT DEPLOYED CODE CHANGES (git_diff.patch) ===
{evidence_bundle['git_diff'] or 'No git diff provided.'}

=== APPLICATION LOGS (logs/application.log) ===
{evidence_bundle['logs'] or 'No logs provided.'}

=== SYSTEM METRICS (metrics/metrics.csv) ===
{evidence_bundle['metrics'] or 'No metrics provided.'}

=== SERVICE SOURCE CODE ===
{source_code_section or 'No source files provided.'}

=== INFRASTRUCTURE CONFIGURATION (docker-compose.yml) ===
{evidence_bundle['config'] or 'No config provided.'}

=== INSTRUCTIONS ===
1. Analyze the timeline and causal relationship between the git diff, logs, and telemetry metrics.
2. Formulate your single best explanation of the underlying root cause.
3. Cite concrete evidence from the logs, metrics, or source code.
4. Output your response as a strictly valid JSON object matching this schema:
{{
  "incident_id": "{incident_id}",
  "root_cause_guess": "<Concise, precise explanation of the underlying technical root cause>",
  "reasoning": "<Step-by-step causal reasoning explaining how the change led to the symptoms>",
  "confidence": <Confidence score between 0.0 and 1.0>,
  "evidence": [
    {{
      "source": "<logs | metrics | code | git_diff>",
      "reference": "<line number, timestamp, metric column, or function name>",
      "excerpt": "<exact cited snippet>"
    }}
  ],
  "suggested_mitigation": "<Actionable mitigation recommendation>"
}}
"""
        return prompt

    def diagnose(self, incident_dir: Path | str) -> Dict[str, Any]:
        """Perform single-call baseline diagnosis on the specified incident bundle.

        Args:
            incident_dir: Path to the incident directory.

        Returns:
            Structured dictionary matching baseline_schema.json.
        """
        path = Path(incident_dir)
        evidence = self.load_incident_evidence(path)
        prompt = self._build_prompt(evidence)

        system_prompt = (
            "You are an expert site reliability and backend software engineer. "
            "Your task is to analyze production incident artifacts and produce an unverified, single-shot root-cause diagnosis. "
            "You must respond ONLY with a valid JSON object conforming to the requested schema."
        )

        # Make EXACTLY ONE primary Groq LLM call
        response = self.llm_client.generate_structured(
            prompt=prompt,
            schema=self.schema,
            system_prompt=system_prompt,
            temperature=0.0,
        )

        result = response.get_structured()

        # Normalize and validate fields
        normalized = self._normalize_and_validate(result, evidence["incident_id"])
        return normalized

    def _normalize_and_validate(self, data: Dict[str, Any], expected_id: str) -> Dict[str, Any]:
        """Normalize field aliases and validate against baseline JSON schema."""
        # Handle field alias root_cause -> root_cause_guess
        if "root_cause" in data and "root_cause_guess" not in data:
            data["root_cause_guess"] = data.pop("root_cause")

        # Guarantee matching incident ID
        data["incident_id"] = expected_id

        # Ensure confidence is clamped float
        try:
            data["confidence"] = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (ValueError, TypeError):
            data["confidence"] = 0.5

        # Ensure reasoning is string
        if not data.get("reasoning"):
            data["reasoning"] = "No reasoning provided."

        # Ensure root_cause_guess is non-empty string
        if not data.get("root_cause_guess"):
            data["root_cause_guess"] = "Undetermined root cause."

        # Normalize evidence array if present
        if "evidence" not in data or not isinstance(data["evidence"], list):
            data["evidence"] = []
        else:
            cleaned_evidence = []
            for item in data["evidence"]:
                if isinstance(item, dict):
                    cleaned_evidence.append({
                        "source": str(item.get("source", "unknown")),
                        "reference": str(item.get("reference", "N/A")),
                        "excerpt": str(item.get("excerpt", "")),
                    })
            data["evidence"] = cleaned_evidence

        # Validate strictly against baseline_schema.json
        jsonschema.validate(instance=data, schema=self.schema)
        return data


def main() -> None:
    parser = argparse.ArgumentParser(description="Sentinel Single-Shot Baseline Investigator")
    parser.add_argument(
        "incident_dir",
        type=str,
        help="Path to incident bundle directory (e.g., incidents/inc_01_n_plus_one_query)",
    )
    parser.add_argument(
        "--output",
        type=str,
        default=None,
        help="Optional path to save JSON diagnosis output",
    )
    args = parser.parse_args()

    try:
        agent = BaselineAgent()
        print(f"[Baseline] Investigating incident at: {args.incident_dir}")
        diagnosis = agent.diagnose(args.incident_dir)

        formatted_json = json.dumps(diagnosis, indent=2)
        print("\n=== BASELINE DIAGNOSIS RESULT ===")
        print(formatted_json)

        if args.output:
            out_path = Path(args.output)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(formatted_json, encoding="utf-8")
            print(f"\n[Baseline] Saved result to: {args.output}")

    except LLMConfigurationError as e:
        print(f"\n[Configuration Error] {e}")
        print("Tip: Add GROQ_API_KEY=your_key to your .env file or environment.")
        exit(1)
    except LLMError as e:
        print(f"\n[LLM Error] Diagnosis failed: {e}")
        exit(1)



if __name__ == "__main__":
    main()
