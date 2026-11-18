"""Sentinel 2.0 external integrations package (Mission 27).

Provides the read-only GitHub REST API client used to enrich incident
investigations with repository context, and the durable persistence layer for
GitHub repository connections (Mission 27.3.1):

- GitHubClient: minimal, read-only client restricted to api.github.com.
- FilesystemIntegrationStore / PostgresIntegrationStore: non-secret connection
  persistence behind the IntegrationStore interface.

The client is intentionally credential-lazy: a missing SENTINEL_GITHUB_TOKEN
surfaces as a clear configuration error only when a GitHub operation is
attempted, so unconfigured deployments keep working unchanged. Connection
persistence stores only non-secret metadata and never requires a token.
"""

from __future__ import annotations

from core.integrations.factory import get_integration_store
from core.integrations.filesystem_store import FilesystemIntegrationStore
from core.integrations.github_client import (
    ENV_GITHUB_TOKEN,
    GITHUB_API_BASE_URL,
    GITHUB_API_HOST,
    GITHUB_API_PORT,
    GITHUB_API_SCHEME,
    GITHUB_API_VERSION,
    GitHubAuthenticationError,
    GitHubBranch,
    GitHubClient,
    GitHubCommit,
    GitHubConfigurationError,
    GitHubError,
    GitHubFileContent,
    GitHubInvalidResponseError,
    GitHubNotFoundError,
    GitHubPermissionError,
    GitHubPullRequest,
    GitHubRateLimitError,
    GitHubRepository,
    GitHubUnavailableError,
    GitHubValidationError,
    redact_secret,
)
from core.integrations.postgres_store import PostgresIntegrationStore
from core.integrations.repository_store import (
    CONNECTION_STATUSES,
    CONNECTION_VISIBILITIES,
    GITHUB_CREDENTIAL_REF,
    PROVIDER_GITHUB,
    STATUS_CONNECTED,
    STATUS_DISABLED,
    STATUS_ERROR,
    DuplicateConnectionError,
    GitHubConnection,
    IntegrationStore,
    IntegrationStoreError,
    validate_connection_id,
)

__all__ = [
    # Configuration
    "ENV_GITHUB_TOKEN",
    "GITHUB_API_BASE_URL",
    "GITHUB_API_HOST",
    "GITHUB_API_SCHEME",
    "GITHUB_API_PORT",
    "GITHUB_API_VERSION",
    # Client
    "GitHubClient",
    # Response models
    "GitHubRepository",
    "GitHubBranch",
    "GitHubCommit",
    "GitHubFileContent",
    "GitHubPullRequest",
    # Errors
    "GitHubError",
    "GitHubConfigurationError",
    "GitHubValidationError",
    "GitHubAuthenticationError",
    "GitHubPermissionError",
    "GitHubNotFoundError",
    "GitHubRateLimitError",
    "GitHubUnavailableError",
    "GitHubInvalidResponseError",
    # Utilities
    "redact_secret",
    # Connection persistence (Mission 27.3.1)
    "GITHUB_CREDENTIAL_REF",
    "PROVIDER_GITHUB",
    "STATUS_CONNECTED",
    "STATUS_ERROR",
    "STATUS_DISABLED",
    "CONNECTION_STATUSES",
    "CONNECTION_VISIBILITIES",
    "GitHubConnection",
    "IntegrationStore",
    "FilesystemIntegrationStore",
    "PostgresIntegrationStore",
    "get_integration_store",
    "validate_connection_id",
    "IntegrationStoreError",
    "DuplicateConnectionError",
]
