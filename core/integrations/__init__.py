"""Sentinel 2.0 external integrations package (Mission 27).

Currently provides the read-only GitHub REST API client used to enrich
incident investigations with repository context:

- GitHubClient: minimal, read-only client restricted to api.github.com.
- Typed response models for repositories, branches, commits, files and PRs.
- A sanitised error hierarchy and defensive token redaction.

The client is intentionally credential-lazy: a missing SENTINEL_GITHUB_TOKEN
surfaces as a clear configuration error only when a GitHub operation is
attempted, so unconfigured deployments keep working unchanged.
"""

from __future__ import annotations

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
]
