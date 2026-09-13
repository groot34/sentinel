"""Tool registry and execution manager for Sentinel 2.0.

Provides discovery, registration, and safe execution management for all tools.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional
from pydantic import BaseModel

from core.tools.base import BaseTool
from core.tools.context import ToolExecutionContext
from core.tools.errors import (
    ToolNotFoundError,
    ToolRegistrationError,
    ToolSafetyError,
    ToolValidationError,
)
from core.tools.result import ToolResult


class ToolRegistry:
    """Central registry for discovering, cataloging, and executing tools."""

    def __init__(self) -> None:
        self._tools: Dict[str, BaseTool[Any]] = {}

    def register(self, tool: BaseTool[Any], override: bool = False) -> None:
        """Register a tool instance.

        Args:
            tool: BaseTool instance to register.
            override: If True, overwrite an existing registration for the same tool name.

        Raises:
            ToolRegistrationError: If tool with same name is already registered and override is False.
        """
        if not isinstance(tool, BaseTool):
            raise ToolRegistrationError(f"Expected BaseTool instance, got {type(tool).__name__}")

        if not tool.name or not tool.name.strip():
            raise ToolRegistrationError("Tool name cannot be empty")

        if tool.name in self._tools and not override:
            raise ToolRegistrationError(
                f"Tool '{tool.name}' is already registered in this registry. "
                "Pass override=True to replace it."
            )

        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool[Any]:
        """Retrieve a tool by name.

        Args:
            name: Tool name to look up.

        Returns:
            Registered BaseTool instance.

        Raises:
            ToolNotFoundError: If tool is not registered.
        """
        if name not in self._tools:
            raise ToolNotFoundError(f"Tool '{name}' is not registered in this registry")
        return self._tools[name]

    def has(self, name: str) -> bool:
        """Check if a tool is registered."""
        return name in self._tools

    def list(self) -> List[str]:
        """List registered tool names in deterministic alphabetical order."""
        return sorted(self._tools.keys())

    def get_schemas(self) -> List[Dict[str, Any]]:
        """Return function-calling JSON schemas for all registered tools in deterministic order."""
        return [self._tools[name].get_json_schema() for name in self.list()]

    def execute(
        self,
        name: str,
        params: BaseModel | Dict[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        """Execute a registered tool within a trusted execution context.

        Handles resolution, input validation, context boundary enforcement,
        timing, and error normalisation.

        Args:
            name: Name of tool to execute.
            params: Tool parameters as a Pydantic model or dictionary.
            context: Trusted execution context provided by Sentinel.

        Returns:
            ToolResult execution envelope.
        """
        start_time = time.perf_counter()

        # 1. Resolve tool
        if name not in self._tools:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.error(
                tool_name=name,
                error=f"Tool not found in registry: '{name}'",
                execution_time_ms=elapsed_ms,
            )

        tool = self._tools[name]

        # 2. Validate context
        if not isinstance(context, ToolExecutionContext):
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.error(
                tool_name=name,
                error=f"Invalid execution context: expected ToolExecutionContext, got {type(context).__name__}",
                execution_time_ms=elapsed_ms,
            )

        # 3. Validate input against tool input schema
        try:
            validated_params = tool.validate_input(params)
        except ToolValidationError as ve:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.fail(
                tool_name=name,
                error=str(ve),
                execution_time_ms=elapsed_ms,
            )

        # 4. Execute tool
        try:
            result = tool.run(validated_params, context)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            # Ensure execution_time_ms is populated
            if result.execution_time_ms == 0.0:
                return result.model_copy(update={"execution_time_ms": elapsed_ms})
            return result

        except ToolSafetyError as se:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.error(
                tool_name=name,
                error=f"Safety violation: {se}",
                execution_time_ms=elapsed_ms,
            )
        except FileNotFoundError as fnf:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.fail(
                tool_name=name,
                error=f"Target file not found: {fnf}",
                execution_time_ms=elapsed_ms,
            )
        except Exception as exc:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            return ToolResult.error(
                tool_name=name,
                error=f"Unexpected tool execution error: {exc}",
                execution_time_ms=elapsed_ms,
            )
