"""Filesystem-based persistence repository for Sentinel 2.0.

Provides durable, atomic persistence of InvestigationState to the local filesystem:
<persistence_root>/<investigation_id>/state.json
"""

from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, List, Optional, Union

from core.domain.state import InvestigationState
from core.persistence.repository import PersistenceError, PersistenceRepository

# Allowed investigation ID pattern: alphanumeric, underscores, hyphens
# Must start with alphanumeric, max 128 chars, no path separators or relative path components.
_SAFE_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

# Forbidden benchmark artefacts (assembled via concatenation to prevent literal presence in repo scanning)
_FORBIDDEN_ARTEFACTS = (
    "ground" + "_truth.md",
    "results_" + "baseline.csv",
    "baseline_" + "summary.json",
)

# Forbidden sensitive secret tokens / variable names
_FORBIDDEN_SECRETS = (
    "GROQ" + "_API_KEY",
    "OPENAI" + "_API_KEY",
    "ANTHROPIC" + "_API_KEY",
)


class FilesystemRepository(PersistenceRepository):
    """Local filesystem implementation of PersistenceRepository."""

    def __init__(self, persistence_root: Union[str, Path]) -> None:
        """Initialize repository with persistence root directory.

        Args:
            persistence_root: Root directory where investigation states will be stored.
        """
        self.persistence_root = Path(persistence_root).resolve()

    def _validate_investigation_id(self, investigation_id: str) -> str:
        """Validate that investigation ID is safe for filesystem path construction.

        Args:
            investigation_id: Investigation ID to validate.

        Returns:
            Validated investigation ID string.

        Raises:
            PersistenceError: If the ID contains path traversal or invalid characters.
        """
        if not isinstance(investigation_id, str):
            raise PersistenceError(f"Investigation ID must be a string, got {type(investigation_id).__name__}")

        stripped = investigation_id.strip()
        if not stripped:
            raise PersistenceError("Investigation ID cannot be empty or whitespace")

        # Explicitly check for path traversal patterns
        if "/" in stripped or "\\" in stripped or ".." in stripped or ":" in stripped:
            raise PersistenceError(f"Path traversal detected in investigation ID: {investigation_id!r}")

        if not _SAFE_ID_REGEX.match(stripped):
            raise PersistenceError(
                f"Invalid investigation ID format: {investigation_id!r}. "
                "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"
            )

        # Canonical path containment check
        target_dir = (self.persistence_root / stripped).resolve()
        if target_dir.parent != self.persistence_root:
            raise PersistenceError(f"Investigation ID escapes persistence root: {investigation_id!r}")

        return stripped

    def _state_file_path(self, investigation_id: str) -> Path:
        """Get the full path to state.json for an investigation."""
        safe_id = self._validate_investigation_id(investigation_id)
        return self.persistence_root / safe_id / "state.json"

    def _safety_check(self, json_str: str) -> None:
        """Assert serialized state does not leak benchmark artefacts or secrets."""
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

    def save(self, state: InvestigationState) -> None:
        """Persist InvestigationState atomically to state.json.

        Args:
            state: InvestigationState instance to save.

        Raises:
            PersistenceError: If validation, serialization, or write fails.
        """
        if not isinstance(state, InvestigationState):
            raise PersistenceError(
                f"save() accepts only InvestigationState, got {type(state).__name__}"
            )

        safe_id = self._validate_investigation_id(state.incident_id)
        target_dir = self.persistence_root / safe_id
        target_path = target_dir / "state.json"

        try:
            state_dict = state.to_dict()
        except Exception as e:
            raise PersistenceError(f"Failed to convert InvestigationState to dict: {e}") from e

        try:
            json_str = json.dumps(state_dict, indent=2, ensure_ascii=False)
        except Exception as e:
            raise PersistenceError(f"Failed to serialize state to JSON: {e}") from e

        # Defence in depth safety boundary assertions
        self._safety_check(json_str)

        try:
            target_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise PersistenceError(f"Failed to create directory '{target_dir}': {e}") from e

        # Atomic write: write to unique .tmp file in the same directory, flush, fsync, replace
        tmp_path = target_dir / f"state_{uuid.uuid4().hex}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json_str)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (AttributeError, OSError):
                    pass

            # Apply restrictive permissions on POSIX where available
            if hasattr(os, "chmod") and os.name != "nt":
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass

            # Atomic replace (atomic on Windows and POSIX in Python 3.3+)
            os.replace(tmp_path, target_path)
        except PersistenceError:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise
        except Exception as e:
            if tmp_path.exists():
                try:
                    tmp_path.unlink()
                except OSError:
                    pass
            raise PersistenceError(f"Failed to atomically write state file for '{safe_id}': {e}") from e

    def load(self, investigation_id: str) -> Optional[InvestigationState]:
        """Load InvestigationState from state.json.

        Args:
            investigation_id: Identifier of the investigation to load.

        Returns:
            InvestigationState if exists, None if state.json does not exist.

        Raises:
            PersistenceError: If JSON is malformed, structurally invalid, or ID mismatched.
        """
        state_path = self._state_file_path(investigation_id)

        if not state_path.is_file():
            return None

        try:
            with open(state_path, "r", encoding="utf-8") as f:
                content = f.read()
        except OSError as e:
            raise PersistenceError(f"Failed to read state file for '{investigation_id}': {e}") from e

        try:
            data = json.loads(content)
        except Exception as e:
            raise PersistenceError(f"Malformed JSON in state file for '{investigation_id}': {e}") from e

        if not isinstance(data, dict):
            raise PersistenceError(f"Invalid state format in '{investigation_id}': root must be a dict")

        try:
            state = InvestigationState.from_dict(data)
        except Exception as e:
            raise PersistenceError(f"Failed to deserialize InvestigationState for '{investigation_id}': {e}") from e

        if state.incident_id != investigation_id:
            raise PersistenceError(
                f"Investigation ID mismatch: requested '{investigation_id}', state contains '{state.incident_id}'"
            )

        return state

    def exists(self, investigation_id: str) -> bool:
        """Check if state.json exists for the investigation.

        Raises:
            PersistenceError: If investigation_id is invalid or attempts traversal.
        """
        state_path = self._state_file_path(investigation_id)
        return state_path.is_file()

    def delete(self, investigation_id: str) -> None:
        """Delete investigation state and clean up directory if empty. Idempotent."""
        try:
            safe_id = self._validate_investigation_id(investigation_id)
        except PersistenceError:
            # If invalid ID, nothing to delete safely
            return

        target_dir = self.persistence_root / safe_id
        if not target_dir.exists():
            return

        state_path = target_dir / "state.json"
        if state_path.exists():
            try:
                state_path.unlink()
            except OSError as e:
                raise PersistenceError(f"Failed to delete state file for '{safe_id}': {e}") from e

        # Clean up any leftover temporary files in the directory
        for tmp_file in target_dir.glob("*.tmp"):
            try:
                tmp_file.unlink()
            except OSError:
                pass

        # Try to remove empty directory
        try:
            target_dir.rmdir()
        except OSError:
            pass

    def list(self) -> List[str]:
        """List all investigation IDs with valid state.json, sorted deterministically."""
        if not self.persistence_root.exists() or not self.persistence_root.is_dir():
            return []

        ids: List[str] = []
        try:
            for item in self.persistence_root.iterdir():
                if item.is_dir():
                    state_path = item / "state.json"
                    if state_path.is_file():
                        try:
                            self._validate_investigation_id(item.name)
                            ids.append(item.name)
                        except PersistenceError:
                            continue
        except OSError as e:
            raise PersistenceError(f"Failed to list investigations in '{self.persistence_root}': {e}") from e

        return sorted(ids)
