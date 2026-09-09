"""Persistence repository abstraction for Sentinel 2.0.

Provides an abstract interface for durable investigation state persistence.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from core.domain.state import InvestigationState


class PersistenceError(Exception):
    """Base exception for all persistence failures."""
    pass


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
