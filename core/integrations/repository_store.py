"""Persistence abstraction for GitHub integration connections (Mission 27.3.1).

Defines the durable, non-secret record that represents a connected GitHub
repository, and the backend-agnostic :class:`IntegrationStore` interface used by
the filesystem and PostgreSQL implementations.

Security posture
----------------
A connection record deliberately stores **only non-secret metadata**. It never
holds the GitHub credential itself - only a non-secret reference
(:data:`GITHUB_CREDENTIAL_REF`) that names the environment variable the token is
read from at request time. The model rejects any other ``credential_ref`` value,
and a defensive safety check refuses to serialise anything that looks like a
live GitHub token, so a credential can never be persisted by accident.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from core.integrations.github_client import ENV_GITHUB_TOKEN
from core.persistence.repository import PersistenceError

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class IntegrationStoreError(PersistenceError):
    """Base exception for integration-persistence failures.

    Subclasses :class:`core.persistence.repository.PersistenceError` so callers
    that already handle persistence failures uniformly keep working, while code
    that cares about integration connections can catch the narrower type.
    """


class DuplicateConnectionError(IntegrationStoreError):
    """Raised when a connection ID or an (owner, repository) pair already exists."""


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: The only supported integration provider.
PROVIDER_GITHUB = "github"

#: The only accepted, non-secret credential reference. It names the environment
#: variable the token is read from; the token value itself is never stored.
GITHUB_CREDENTIAL_REF = f"env:{ENV_GITHUB_TOKEN}"

#: Lifecycle statuses proposed by the Mission 27.1 audit.
STATUS_CONNECTED = "connected"
STATUS_ERROR = "error"
STATUS_DISABLED = "disabled"
CONNECTION_STATUSES = frozenset({STATUS_CONNECTED, STATUS_ERROR, STATUS_DISABLED})

#: Accepted repository visibility values.
CONNECTION_VISIBILITIES = frozenset({"public", "private", "internal"})

#: Connection IDs must be safe to use as a filesystem path segment.
_CONNECTION_ID_REGEX = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$")

#: GitHub owner / repository name shape (conservative, traversal-safe subset).
_OWNER_REPO_REGEX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")

#: Token-looking patterns, matched defensively so a credential can never be
#: serialised into a connection record.
_TOKEN_PATTERNS = (
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
)


def _utcnow_iso() -> str:
    """Return the current UTC time as an ISO-8601 string."""
    return datetime.now(tz=timezone.utc).isoformat()


def validate_connection_id(connection_id: str) -> str:
    """Validate that a connection ID is a safe identifier.

    Args:
        connection_id: Connection ID to validate.

    Returns:
        The validated, stripped connection ID.

    Raises:
        IntegrationStoreError: If the ID is not a string, is empty, contains path
            traversal characters, or does not match the safe-ID pattern.
    """
    if not isinstance(connection_id, str):
        raise IntegrationStoreError(
            f"Connection ID must be a string, got {type(connection_id).__name__}"
        )

    stripped = connection_id.strip()
    if not stripped:
        raise IntegrationStoreError("Connection ID cannot be empty or whitespace")

    if "/" in stripped or "\\" in stripped or ".." in stripped or ":" in stripped:
        raise IntegrationStoreError(f"Path traversal detected in connection ID: {connection_id!r}")

    if not _CONNECTION_ID_REGEX.match(stripped):
        raise IntegrationStoreError(
            f"Invalid connection ID format: {connection_id!r}. "
            "Must match ^[a-zA-Z0-9][a-zA-Z0-9_-]{0,127}$"
        )

    return stripped


def check_connection_safety(json_str: str) -> None:
    """Assert a serialised connection record does not embed a credential value.

    Args:
        json_str: Serialised JSON connection record.

    Raises:
        IntegrationStoreError: If a GitHub-token-shaped value is present.
    """
    for pattern in _TOKEN_PATTERNS:
        if pattern.search(json_str):
            raise IntegrationStoreError(
                "Safety check failed: a credential-looking value was detected in a "
                "connection record. Tokens must never be persisted."
            )


def _validate_owner_or_repo(value: str, field_name: str) -> str:
    """Validate a GitHub owner or repository name."""
    if not isinstance(value, str):
        raise IntegrationStoreError(f"{field_name} must be a string, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        raise IntegrationStoreError(f"{field_name} cannot be empty")
    if "/" in stripped or "\\" in stripped or ".." in stripped:
        raise IntegrationStoreError(f"Path traversal detected in {field_name}: {value!r}")
    if not _OWNER_REPO_REGEX.match(stripped):
        raise IntegrationStoreError(f"Invalid {field_name} format: {value!r}")
    return stripped


def _validate_timestamp(value: Optional[str], field_name: str, *, required: bool) -> Optional[str]:
    """Validate that a timestamp field is an ISO-8601 string.

    This is the single source of truth for timestamp validity, shared by every
    backend so that filesystem and PostgreSQL accept and reject exactly the same
    values on create and update.

    Args:
        value: Timestamp value to validate (may be None when not required).
        field_name: Field name used in error messages.
        required: When True, None is rejected.

    Returns:
        The validated value (or None when optional and absent).

    Raises:
        IntegrationStoreError: If the value is missing when required, or is not a
            non-empty ISO-8601 string.
    """
    if value is None:
        if required:
            raise IntegrationStoreError(f"{field_name} must be a non-empty string")
        return None
    if not isinstance(value, str) or not value.strip():
        raise IntegrationStoreError(f"{field_name} must be a non-empty string")
    try:
        datetime.fromisoformat(value)
    except ValueError as e:
        raise IntegrationStoreError(
            f"{field_name} must be an ISO-8601 timestamp: {value!r}"
        ) from e
    return value


# ---------------------------------------------------------------------------
# Connection record
# ---------------------------------------------------------------------------


@dataclass
class GitHubConnection:
    """Non-secret metadata describing a connected GitHub repository.

    Only the fields listed here are persisted. No GitHub token - nor any other
    credential value - is ever part of this model; ``credential_ref`` is a
    non-secret pointer to the environment variable the token is read from.
    """

    connection_id: str
    label: str
    owner: str
    repo: str
    default_branch: str = "main"
    visibility: str = "private"
    provider: str = PROVIDER_GITHUB
    status: str = STATUS_CONNECTED
    credential_ref: str = GITHUB_CREDENTIAL_REF
    scopes: List[str] = field(default_factory=list)
    last_verified_at: Optional[str] = None
    created_at: str = field(default_factory=_utcnow_iso)
    updated_at: str = field(default_factory=_utcnow_iso)

    def __post_init__(self) -> None:
        if not isinstance(self.connection_id, str):
            raise IntegrationStoreError("connection_id must be a string")
        validate_connection_id(self.connection_id)

        if self.provider != PROVIDER_GITHUB:
            raise IntegrationStoreError(
                f"Unsupported provider {self.provider!r}; only {PROVIDER_GITHUB!r} is supported."
            )

        if not isinstance(self.label, str) or not self.label.strip():
            raise IntegrationStoreError("label cannot be empty")

        _validate_owner_or_repo(self.owner, "owner")
        _validate_owner_or_repo(self.repo, "repo")

        if not isinstance(self.default_branch, str) or not self.default_branch.strip():
            raise IntegrationStoreError("default_branch cannot be empty")
        if ".." in self.default_branch or any(c in self.default_branch for c in "\r\n\t"):
            raise IntegrationStoreError(f"Invalid default_branch: {self.default_branch!r}")

        if self.visibility not in CONNECTION_VISIBILITIES:
            raise IntegrationStoreError(
                f"Invalid visibility {self.visibility!r}; "
                f"expected one of {sorted(CONNECTION_VISIBILITIES)}"
            )

        if self.status not in CONNECTION_STATUSES:
            raise IntegrationStoreError(
                f"Invalid status {self.status!r}; expected one of {sorted(CONNECTION_STATUSES)}"
            )

        # Security: the only accepted credential reference is the fixed,
        # non-secret env pointer. A raw token supplied here is rejected.
        if self.credential_ref != GITHUB_CREDENTIAL_REF:
            raise IntegrationStoreError(
                f"credential_ref must be the non-secret reference {GITHUB_CREDENTIAL_REF!r}; "
                "raw credentials are never accepted or persisted."
            )

        if not isinstance(self.scopes, list) or not all(
            isinstance(s, str) and s.strip() for s in self.scopes
        ):
            raise IntegrationStoreError("scopes must be a list of non-empty strings")

        _validate_timestamp(self.last_verified_at, "last_verified_at", required=False)
        _validate_timestamp(self.created_at, "created_at", required=True)
        _validate_timestamp(self.updated_at, "updated_at", required=True)

    def to_dict(self) -> Dict[str, Any]:
        """Return a JSON-serialisable representation of the connection."""
        return {
            "connection_id": self.connection_id,
            "provider": self.provider,
            "label": self.label,
            "owner": self.owner,
            "repo": self.repo,
            "default_branch": self.default_branch,
            "visibility": self.visibility,
            "status": self.status,
            "credential_ref": self.credential_ref,
            "scopes": list(self.scopes),
            "last_verified_at": self.last_verified_at,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GitHubConnection":
        """Reconstruct and validate a connection from a persisted mapping.

        Args:
            data: Mapping previously produced by :meth:`to_dict`.

        Returns:
            A validated :class:`GitHubConnection`.

        Raises:
            IntegrationStoreError: If *data* is not a mapping or a field is invalid.
        """
        if not isinstance(data, dict):
            raise IntegrationStoreError(
                f"Invalid connection record: root must be an object, got {type(data).__name__}"
            )

        required = ("connection_id", "label", "owner", "repo")
        missing = [key for key in required if data.get(key) in (None, "")]
        if missing:
            raise IntegrationStoreError(
                f"Invalid connection record: missing required field(s) {sorted(missing)}"
            )

        try:
            return cls(
                connection_id=data["connection_id"],
                label=data["label"],
                owner=data["owner"],
                repo=data["repo"],
                default_branch=data.get("default_branch", "main"),
                visibility=data.get("visibility", "private"),
                provider=data.get("provider", PROVIDER_GITHUB),
                status=data.get("status", STATUS_CONNECTED),
                credential_ref=data.get("credential_ref", GITHUB_CREDENTIAL_REF),
                scopes=data.get("scopes") or [],
                last_verified_at=data.get("last_verified_at"),
                created_at=data.get("created_at") or _utcnow_iso(),
                updated_at=data.get("updated_at") or _utcnow_iso(),
            )
        except IntegrationStoreError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            raise IntegrationStoreError(f"Invalid connection record: {e}") from e


# ---------------------------------------------------------------------------
# Store interface
# ---------------------------------------------------------------------------


class IntegrationStore(ABC):
    """Abstract persistence store for GitHub repository connections.

    Both the filesystem and PostgreSQL backends implement this identical
    interface so callers never depend on the concrete backend.
    """

    @abstractmethod
    def create(self, connection: GitHubConnection) -> None:
        """Persist a new connection.

        Args:
            connection: Validated connection record to store.

        Raises:
            DuplicateConnectionError: If a connection with the same ID, or the
                same (owner, repo) pair, already exists.
            IntegrationStoreError: If validation or writing fails.
        """
        raise NotImplementedError

    @abstractmethod
    def get(self, connection_id: str) -> Optional[GitHubConnection]:
        """Retrieve a connection by ID.

        Returns:
            The stored connection, or None when no such connection exists.

        Raises:
            IntegrationStoreError: If the ID is invalid or the record is malformed.
        """
        raise NotImplementedError

    @abstractmethod
    def list(self) -> List[GitHubConnection]:
        """Return all connections ordered deterministically by connection ID."""
        raise NotImplementedError

    @abstractmethod
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
        """Update mutable metadata and/or status of an existing connection.

        Only the supplied (non-None) fields are changed; ``updated_at`` is
        refreshed automatically. Immutable fields (ID, provider, owner, repo,
        credential_ref) are never modified.

        Args:
            connection_id: ID of the connection to update.
            label: Optional new label.
            status: Optional new status (one of connected/error/disabled).
            default_branch: Optional new default branch.
            visibility: Optional new visibility.
            scopes: Optional replacement scope list.
            last_verified_at: Optional new last-verified timestamp.

        Returns:
            The updated connection.

        Raises:
            IntegrationStoreError: If the connection does not exist or a value is invalid.
        """
        raise NotImplementedError

    @abstractmethod
    def delete(self, connection_id: str) -> None:
        """Delete a connection. Idempotent: deleting a missing ID is a no-op."""
        raise NotImplementedError

    @abstractmethod
    def exists(self, connection_id: str) -> bool:
        """Return True when a connection with the given ID exists."""
        raise NotImplementedError

    def close(self) -> None:
        """Release resources owned by the store.

        Default is a no-op; backends that own a pool override it. Implementations
        must tolerate being called more than once.
        """
        return None
