"""Persistence package for Sentinel 2.0.

Provides durable repository interfaces and implementations for InvestigationState.
"""

from core.persistence.filesystem import FilesystemRepository
from core.persistence.repository import PersistenceError, PersistenceRepository

__all__ = [
    "PersistenceError",
    "PersistenceRepository",
    "FilesystemRepository",
]
