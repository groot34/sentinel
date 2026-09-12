"""Tool error hierarchy for Sentinel 2.0.

Defines typed exceptions for the tool abstraction layer.
"""

from __future__ import annotations


class ToolError(Exception):
    """Base exception for all tool-related errors."""
    pass


class ToolRegistrationError(ToolError):
    """Raised when a tool registration fails or conflicts."""
    pass


class ToolNotFoundError(ToolError):
    """Raised when a requested tool is not found in the registry."""
    pass


class ToolValidationError(ToolError):
    """Raised when tool input validation fails."""
    pass


class ToolSafetyError(ToolError):
    """Raised when a safety policy, path boundary, or forbidden resource is violated."""
    pass
