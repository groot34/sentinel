"""Trusted execution context for Sentinel 2.0 tools.

Defines the security boundary and path resolution rules for tool execution.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Union

from core.tools.errors import ToolSafetyError

# Forbidden benchmark artefacts and ground truth files
_FORBIDDEN_FILES = frozenset([
    "ground_truth.md",
    "results_baseline.csv",
    "baseline_summary.json",
])

# Forbidden sensitive environment tokens
_FORBIDDEN_SECRETS = frozenset([
    "GROQ_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
])


@dataclass(frozen=True)
class ToolExecutionContext:
    """Trusted runtime execution context owned and provided exclusively by Sentinel.

    The tool caller (agent or future LLM) may control WHAT to inspect (parameters,
    relative path), but Sentinel strictly controls WHERE execution occurs (incident_root).
    """

    incident_id: str
    incident_root: Path

    def __post_init__(self) -> None:
        if not isinstance(self.incident_id, str) or not self.incident_id.strip():
            raise ToolSafetyError("incident_id must be a non-empty string")

        if not self.incident_root:
            raise ToolSafetyError("incident_root cannot be empty")

        root = Path(self.incident_root).resolve()
        if not root.exists():
            raise ToolSafetyError(f"Incident root directory does not exist: {root}")
        if not root.is_dir():
            raise ToolSafetyError(f"Incident root path is not a directory: {root}")

        # Set resolved root on frozen dataclass
        object.__setattr__(self, "incident_root", root)

    def resolve_path(self, relative_path: Union[str, Path]) -> Path:
        """Resolve and validate a relative path within the trusted incident boundary.

        Args:
            relative_path: Relative path within the incident bundle.

        Returns:
            Resolved, canonical Path safely contained within incident_root.

        Raises:
            ToolSafetyError: If path attempts traversal, is absolute, or targets forbidden files.
        """
        if not relative_path:
            raise ToolSafetyError("Target path cannot be empty or None")

        raw_str = str(relative_path).strip()
        if not raw_str:
            raise ToolSafetyError("Target path cannot be whitespace")

        # 1. Path traversal pattern checks
        if ".." in raw_str:
            raise ToolSafetyError(f"Path traversal detected in relative path: {raw_str!r}")

        # 2. Absolute path rejection (POSIX / and Windows drive C:\ or UNC \\)
        if raw_str.startswith(("/", "\\")) or (len(raw_str) >= 2 and raw_str[1] == ":"):
            raise ToolSafetyError(f"Absolute path rejected: {raw_str!r}. Only relative paths are permitted.")

        # 3. Forbidden benchmark artefact protection
        path_obj = Path(raw_str)
        for part in path_obj.parts:
            if part in _FORBIDDEN_FILES:
                raise ToolSafetyError(f"Access to forbidden benchmark file is rejected: {part!r}")

        # 4. Canonical path resolution & containment check
        target_path = (self.incident_root / path_obj).resolve()

        try:
            target_path.relative_to(self.incident_root)
        except ValueError:
            raise ToolSafetyError(
                f"Path escapes trusted incident boundary: {raw_str!r} "
                f"(resolved to {target_path}, root is {self.incident_root})"
            )

        # 5. Verify resolved filename against forbidden artefacts (in case of symlinks or aliases)
        if target_path.name in _FORBIDDEN_FILES:
            raise ToolSafetyError(f"Access to forbidden benchmark file is rejected: {target_path.name!r}")

        return target_path
