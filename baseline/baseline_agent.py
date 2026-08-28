"""Baseline Agent: Single-call LLM incident root-cause guesser.

This agent receives the entire raw incident bundle in a single prompt and produces
a one-shot root cause guess without verification tools or structured hypothesis testing.
"""

from typing import Any, Dict


class BaselineAgent:
    """Deliberately simple baseline agent for fair comparison."""

    def __init__(self, model_name: str | None = None) -> None:
        self.model_name = model_name

    def diagnose(self, incident_bundle: Dict[str, Any]) -> Dict[str, Any]:
        """Run single-call baseline diagnosis on the incident bundle.

        Args:
            incident_bundle: Raw incident dictionary containing logs, metrics, code.

        Returns:
            Dictionary matching baseline_schema.json.
        """
        raise NotImplementedError("Baseline diagnosis will be implemented in the next phase.")
