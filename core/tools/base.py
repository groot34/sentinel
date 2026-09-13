"""Base tool interface for Sentinel 2.0.

Provides the abstract definition and input validation contract for all tools.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, Generic, Type, TypeVar
from pydantic import BaseModel, ValidationError

from core.tools.context import ToolExecutionContext
from core.tools.errors import ToolValidationError
from core.tools.result import ToolResult

TInput = TypeVar("TInput", bound=BaseModel)


class BaseTool(ABC, Generic[TInput]):
    """Abstract base class for all Sentinel tools.

    Tools represent deterministic execution primitives that inspect telemetry within
    a trusted ToolExecutionContext.
    """

    name: str
    description: str
    input_schema: Type[TInput]

    def validate_input(self, params: TInput | Dict[str, Any]) -> TInput:
        """Validate an input dictionary or Pydantic model against input_schema.

        Args:
            params: Either an instance of input_schema or a raw dictionary.

        Returns:
            Validated input model instance.

        Raises:
            ToolValidationError: If validation fails.
        """
        if isinstance(params, self.input_schema):
            return params

        if isinstance(params, dict):
            try:
                return self.input_schema.model_validate(params)
            except ValidationError as ve:
                raise ToolValidationError(
                    f"Input validation failed for tool '{self.name}': {ve}"
                ) from ve
            except Exception as exc:
                raise ToolValidationError(
                    f"Unexpected input validation error for tool '{self.name}': {exc}"
                ) from exc

        raise ToolValidationError(
            f"Tool '{self.name}' expected {self.input_schema.__name__} or dict, "
            f"got {type(params).__name__}"
        )

    def get_json_schema(self) -> Dict[str, Any]:
        """Generate standard function-calling JSON Schema for LLM tool selection."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.input_schema.model_json_schema(),
            },
        }

    @abstractmethod
    def run(self, params: TInput, context: ToolExecutionContext) -> ToolResult:
        """Execute the tool deterministically within the trusted execution context.

        Args:
            params: Validated input model.
            context: Trusted execution context containing incident root and ID.

        Returns:
            ToolResult containing observation payload.
        """
        raise NotImplementedError
