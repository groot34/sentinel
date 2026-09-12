"""Persistence repository abstraction for Sentinel 2.0.

Provides an abstract interface for durable investigation state persistence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from core.domain.state import InvestigationState


import re

class PersistenceError(Exception):
    """Base exception for all persistence failures."""
    pass


class ConcurrencyError(PersistenceError):
    """Raised when an optimistic concurrency conflict or concurrent write conflict occurs."""
    pass


_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

_FORBIDDEN_ARTEFACTS = (
    "ground" + "_truth.md",
    "results_" + "baseline.csv",
    "baseline_" + "summary.json",
)

_FORBIDDEN_SECRETS = (
    "GROQ" + "_API_KEY",
    "OPENAI" + "_API_KEY",
    "ANTHROPIC" + "_API_KEY",
)


def validate_investigation_id(investigation_id: str) -> str:
    """Validate that investigation ID is a safe identifier.

    Args:
        investigation_id: Investigation ID to validate.

    Returns:
        Validated, stripped investigation ID string.

    Raises:
        PersistenceError: If the ID contains invalid characters, path traversal, or is malformed.
    """
    if not isinstance(investigation_id, str):
        raise PersistenceError(f"Investigation ID must be a string, got {type(investigation_id).__name__}")

    stripped = investigation_id.strip()
    if not stripped:
        raise PersistenceError("Investigation ID cannot be empty or whitespace")

    if "/" in stripped or "\\" in stripped or ".." in stripped or ":" in stripped:
        raise PersistenceError(f"Path traversal detected in investigation ID: {investigation_id!r}")

    if not _SAFE_ID_REGEX.match(stripped):
        raise PersistenceError(
            f"Invalid investigation ID format: {investigation_id!r}. "
            "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"
        )

    return stripped


def check_persistence_safety(json_str: str) -> None:
    """Assert serialized state does not leak benchmark artefacts or secrets.

    Args:
        json_str: Serialized JSON state string.

    Raises:
        PersistenceError: If forbidden benchmark files or API key secrets are present.
    """
    for forbidden in _FORBIDDEN_ARTEFACTS:
        if forbidden in json_str:
            raise PersistenceError(
                f"Safety check failed: forbidden benchmark artefact detected in state: '{forbidden}'"
            )

    for secret_name in _FORBIDDEN_SECRETS:
        if secret_name in json_str:
            raise PersistenceError(
                f"Safety check failed: forbidden secret key detected in state: '{secret_name}'"
            )


class PersistenceRepository(ABC):
    """Abstract repository for persisting and loading InvestigationState."""

    @abstractmethod
    def save(self, state: InvestigationState) -> None:
        """Persist investigation state idempotently.

        Args:
            state: Valid InvestigationState instance.

        Raises:
            PersistenceError: If serialization, validation, or writing fails.
        """
        raise NotImplementedError

    @abstractmethod
    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        """Load investigation state by ID.

        Args:
            investigation_id: Identifier of the investigation to load.

        Returns:
            InvestigationState if found, None if the state file does not exist.

        Raises:
            PersistenceError: If state exists but is malformed, invalid, or ID mismatched.
        """
        raise NotImplementedError

    @abstractmethod
    def exists(self, investigation_id: str) -> bool:
        """Check if durable investigation state exists.

        Args:
            investigation_id: Identifier to check.

        Returns:
            True if the state file exists, False otherwise.
        """
        raise NotImplementedError

    @abstractmethod
    def delete(self, investigation_id: str) -> None:
        """Delete investigation state and clean up empty directory. Idempotent.

        Args:
            investigation_id: Identifier of investigation to delete.

        Raises:
            PersistenceError: If deletion fails due to I/O or permissions.
        """
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[str]:
        """List all valid investigation IDs in deterministic order.

        Returns:
            Sorted list of investigation IDs that have valid-looking state.
        """
        raise NotImplementedError
