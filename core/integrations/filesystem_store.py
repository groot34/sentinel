"""Filesystem-backed integration store for Sentinel 2.0 (Mission 27.3.1).

Persists one JSON document per GitHub connection under::

    <persistence_root>/integrations/<connection_id>.json

This is the default backend. Writes are atomic (unique temporary file, flush,
``fsync``, then ``os.replace``), records are validated on load, and connection
IDs are validated as safe path segments so traversal outside the store
directory is impossible. Each connection owns its own file, so updating one
connection never disturbs any other record.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path
from typing import List, Optional, Union

from core.integrations.repository_store import (
    DuplicateConnectionError,
    GitHubConnection,
    IntegrationStore,
    IntegrationStoreError,
    _utcnow_iso,
    check_connection_safety,
    validate_connection_id,
)

#: Sub-directory under the persistence root that holds integration records.
_INTEGRATIONS_SUBDIR = "integrations"

#: Module-level registry of create locks, keyed by resolved persistence root.
#: All store instances pointing at the same resolved directory share one lock,
#: so the create() duplicate-scan + write is a single critical section across
#: the whole process for that root. Access is guarded by ``_ROOT_LOCKS_GUARD``
#: so the registry is safe to initialise from concurrent threads.
_ROOT_LOCKS: dict[str, threading.Lock] = {}
_ROOT_LOCKS_GUARD = threading.Lock()


def _lock_for_root(root: Path) -> threading.Lock:
    """Return the process-wide create lock for a resolved persistence root.

    Instances sharing the same resolved directory receive the exact same lock
    object; instances pointing at different directories receive independent
    locks. This is a process-local guarantee only - separate processes sharing
    a root are not synchronised.
    """
    key = str(root)
    with _ROOT_LOCKS_GUARD:
        lock = _ROOT_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _ROOT_LOCKS[key] = lock
        return lock


class FilesystemIntegrationStore(IntegrationStore):
    """Local filesystem implementation of :class:`IntegrationStore`."""

    def __init__(self, persistence_root: Union[str, Path]) -> None:
        """Initialize the store with a persistence root directory.

        Args:
            persistence_root: Root directory under which integration records are
                stored. Records live in an ``integrations/`` sub-directory so they
                never collide with investigation state directories.
        """
        self.persistence_root = Path(persistence_root).resolve()
        self._integrations_dir = self.persistence_root / _INTEGRATIONS_SUBDIR
        # Process-wide create lock shared by every instance pointing at this
        # resolved root (see _lock_for_root). It serialises the duplicate-scan +
        # write in create() so separate instances in the same process cannot
        # both observe an absent record and both write. This is a process-local
        # guarantee only - separate processes sharing a root are not protected
        # (see create()).
        self._create_lock = _lock_for_root(self.persistence_root)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    def _connection_path(self, connection_id: str) -> Path:
        """Return the safe, contained file path for a connection ID."""
        safe_id = validate_connection_id(connection_id)
        base_dir = self._integrations_dir.resolve()
        path = (base_dir / f"{safe_id}.json").resolve()
        if path.parent != base_dir:
            raise IntegrationStoreError(f"Connection ID escapes store root: {connection_id!r}")
        return path

    def _load_record(self, safe_id: str) -> Optional[GitHubConnection]:
        """Load a single connection record, or None when it does not exist.

        Raises:
            IntegrationStoreError: If the file exists but is malformed or invalid.
        """
        path = self._connection_path(safe_id)
        if not path.is_file():
            return None

        try:
            content = path.read_text(encoding="utf-8")
        except OSError as e:
            raise IntegrationStoreError(f"Failed to read connection '{safe_id}': {e}") from e

        try:
            data = json.loads(content)
        except Exception as e:
            raise IntegrationStoreError(f"Malformed JSON in connection file for '{safe_id}': {e}") from e

        connection = GitHubConnection.from_dict(data)

        if connection.connection_id != safe_id:
            raise IntegrationStoreError(
                f"Connection ID mismatch: requested '{safe_id}', "
                f"record contains '{connection.connection_id}'"
            )
        return connection

    def _atomic_write(self, connection: GitHubConnection) -> None:
        """Atomically persist a connection record to disk."""
        try:
            json_str = json.dumps(connection.to_dict(), indent=2, ensure_ascii=False)
        except Exception as e:
            raise IntegrationStoreError(f"Failed to serialize connection '{connection.connection_id}': {e}") from e

        # Defence in depth: never write a credential-looking value.
        check_connection_safety(json_str)

        try:
            self._integrations_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise IntegrationStoreError(
                f"Failed to create integrations directory '{self._integrations_dir}': {e}"
            ) from e

        target_path = self._connection_path(connection.connection_id)
        tmp_path = self._integrations_dir / f"connection_{uuid.uuid4().hex}.tmp"

        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write(json_str)
                f.flush()
                try:
                    os.fsync(f.fileno())
                except (AttributeError, OSError):
                    pass

            if hasattr(os, "chmod") and os.name != "nt":
                try:
                    os.chmod(tmp_path, 0o600)
                except OSError:
                    pass

            os.replace(tmp_path, target_path)
        except IntegrationStoreError:
            self._cleanup_tmp(tmp_path)
            raise
        except Exception as e:
            self._cleanup_tmp(tmp_path)
            raise IntegrationStoreError(
                f"Failed to atomically write connection '{connection.connection_id}': {e}"
            ) from e

    @staticmethod
    def _cleanup_tmp(tmp_path: Path) -> None:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    # IntegrationStore interface
    # ------------------------------------------------------------------

    def create(self, connection: GitHubConnection) -> None:
        """Persist a new connection record.

        Duplicate detection is case-insensitive for (owner, repo), matching
        GitHub's case-insensitive repository naming, while the stored record
        keeps its original casing.

        Concurrency: the duplicate scan and the write are performed as one
        critical section under a process-wide lock shared by every store
        instance pointing at the same resolved persistence root (see
        ``_lock_for_root``), so concurrent threads - whether they share one
        instance or use separate instances on the same root - cannot both
        create a case-variant duplicate. This is a process-local guarantee
        only: there is no cross-process (or cross-host) filesystem lock, so two
        separate processes writing the same persistence root concurrently are
        not protected.
        """
        if not isinstance(connection, GitHubConnection):
            raise IntegrationStoreError(
                f"create() accepts only GitHubConnection, got {type(connection).__name__}"
            )

        safe_id = validate_connection_id(connection.connection_id)

        with self._create_lock:
            if self._load_record(safe_id) is not None:
                raise DuplicateConnectionError(f"Connection '{safe_id}' already exists")

            owner_key = connection.owner.casefold()
            repo_key = connection.repo.casefold()
            for other in self.list():
                if (
                    other.connection_id != safe_id
                    and other.owner.casefold() == owner_key
                    and other.repo.casefold() == repo_key
                ):
                    raise DuplicateConnectionError(
                        f"A connection for {connection.owner}/{connection.repo} already exists "
                        f"as '{other.connection_id}'"
                    )

            self._atomic_write(connection)

    def get(self, connection_id: str) -> Optional[GitHubConnection]:
        """Retrieve a connection by ID, or None when it does not exist."""
        safe_id = validate_connection_id(connection_id)
        return self._load_record(safe_id)

    def list(self) -> List[GitHubConnection]:
        """Return all connections ordered deterministically by connection ID."""
        if not self._integrations_dir.is_dir():
            return []

        try:
            paths = sorted(self._integrations_dir.glob("*.json"))
        except OSError as e:
            raise IntegrationStoreError(
                f"Failed to list integrations in '{self._integrations_dir}': {e}"
            ) from e

        records: List[GitHubConnection] = []
        for path in paths:
            try:
                safe_id = validate_connection_id(path.stem)
            except IntegrationStoreError:
                # Ignore files whose names are not valid connection IDs.
                continue
            record = self._load_record(safe_id)
            if record is not None:
                records.append(record)

        records.sort(key=lambda c: c.connection_id)
        return records

    def update(
        self,
        connection_id: str,
        *,
        label: Optional[str] = None,
        status: Optional[str] = None,
        default_branch: Optional[str] = None,
        visibility: Optional[str] = None,
        scopes: Optional[List[str]] = None,
        last_verified_at: Optional[str] = None,
    ) -> GitHubConnection:
        """Update mutable metadata and/or status of an existing connection."""
        safe_id = validate_connection_id(connection_id)
        current = self._load_record(safe_id)
        if current is None:
            raise IntegrationStoreError(f"Connection '{safe_id}' not found")

        data = current.to_dict()
        if label is not None:
            data["label"] = label
        if status is not None:
            data["status"] = status
        if default_branch is not None:
            data["default_branch"] = default_branch
        if visibility is not None:
            data["visibility"] = visibility
        if scopes is not None:
            data["scopes"] = list(scopes)
        if last_verified_at is not None:
            data["last_verified_at"] = last_verified_at
        data["updated_at"] = _utcnow_iso()

        updated = GitHubConnection.from_dict(data)
        self._atomic_write(updated)
        return updated

    def delete(self, connection_id: str) -> None:
        """Delete a connection. Idempotent."""
        try:
            safe_id = validate_connection_id(connection_id)
        except IntegrationStoreError:
            return

        path = self._connection_path(safe_id)
        if not path.exists():
            return

        try:
            path.unlink()
        except OSError as e:
            raise IntegrationStoreError(f"Failed to delete connection '{safe_id}': {e}") from e

        for tmp_file in self._integrations_dir.glob("connection_*.tmp"):
            try:
                tmp_file.unlink()
            except OSError:
                pass

    def exists(self, connection_id: str) -> bool:
        """Return True when a connection with the given ID exists."""
        safe_id = validate_connection_id(connection_id)
        return self._connection_path(safe_id).is_file()
