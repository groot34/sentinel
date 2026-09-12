"""Persistence package for Sentinel 2.0.

Provides durable repository interfaces and implementations for InvestigationState.
"""

from core.persistence.factory import get_repository
from core.persistence.filesystem import FilesystemRepository
from core.persistence.postgres import PostgresRepository
from core.persistence.repository import ConcurrencyError, PersistenceError, PersistenceRepository

__all__ = [
    "ConcurrencyError",
    "PersistenceError",
    "PersistenceRepository",
    "FilesystemRepository",
    "PostgresRepository",
    "get_repository",
]
