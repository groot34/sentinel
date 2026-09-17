"""Thread-safe factory for Sentinel 2.0 LLM providers."""

from __future__ import annotations

import threading
from typing import Any, Dict, Optional

from core.llm.base import BaseLLMProvider
from core.llm.groq import GroqLLMClient, GroqProvider

_PROVIDER_LOCK = threading.Lock()
_PROVIDER_REGISTRY: Dict[str, BaseLLMProvider] = {}


def get_llm_provider(
    provider_name: str = "groq",
    force_new: bool = False,
    **kwargs: Any,
) -> BaseLLMProvider:
    """Retrieve or instantiate a provider singleton with thread-safe synchronization.

    Lifecycle semantics:
    1. Singletons are cached per provider_name in _PROVIDER_REGISTRY under a threading.Lock.
    2. Requesting 'groq' returns the cached GroqLLMClient instance, preserving active
       session token counters across stages.
    3. Requesting a different provider (e.g. 'mock') caches that instance independently
       under its own key without modifying or resetting the 'groq' singleton.
    4. Passing force_new=True instantiates a fresh provider instance and overwrites the
       cache for that specific provider_name only.

    Args:
        provider_name: Provider key identifier (default: 'groq').
        force_new: If True, replaces existing singleton with a fresh instance.
        **kwargs: Configuration arguments passed to the provider constructor.

    Returns:
        BaseLLMProvider instance.

    Raises:
        ValueError: If provider_name is not supported.
    """
    key = provider_name.strip().lower()
    with _PROVIDER_LOCK:
        if force_new or key not in _PROVIDER_REGISTRY:
            if key == "groq":
                _PROVIDER_REGISTRY[key] = GroqLLMClient(**kwargs)
            else:
                raise ValueError(f"Unsupported LLM provider: '{provider_name}'")
        return _PROVIDER_REGISTRY[key]


def get_llm_client(force_new: bool = False, **kwargs: Any) -> GroqLLMClient:
    """Backward-compatible convenience getter returning GroqLLMClient instance.

    Args:
        force_new: If True, instantiates a fresh GroqLLMClient from environment.
        **kwargs: Optional configuration kwargs.

    Returns:
        Configured GroqLLMClient instance.
    """
    provider = get_llm_provider("groq", force_new=force_new, **kwargs)
    assert isinstance(provider, GroqLLMClient)
    return provider
