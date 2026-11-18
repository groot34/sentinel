-- 005_github_integrations
--
-- Durable, non-secret records describing connected GitHub repositories
-- (Mission 27.3.1). One row per connection.
--
-- Security notes:
--   * No credential is ever stored here. credential_ref holds only a
--     non-secret reference (e.g. 'env:SENTINEL_GITHUB_TOKEN') naming the
--     environment variable the token is read from at request time. There is
--     deliberately no token column.
--   * (owner, repo) is unique: a repository can be connected at most once.
--   * status is constrained to the audited lifecycle values.

CREATE TABLE IF NOT EXISTS github_integrations (
    connection_id VARCHAR(128) PRIMARY KEY,
    provider VARCHAR(32) NOT NULL DEFAULT 'github',
    label VARCHAR(128) NOT NULL,
    owner VARCHAR(100) NOT NULL,
    repo VARCHAR(100) NOT NULL,
    default_branch VARCHAR(255) NOT NULL DEFAULT 'main',
    visibility VARCHAR(16) NOT NULL DEFAULT 'private',
    status VARCHAR(16) NOT NULL DEFAULT 'connected',
    credential_ref VARCHAR(128) NOT NULL DEFAULT 'env:SENTINEL_GITHUB_TOKEN',
    scopes JSONB NOT NULL DEFAULT '[]'::jsonb,
    last_verified_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT github_integrations_status_valid
        CHECK (status IN ('connected', 'error', 'disabled'))
);

-- A repository may be connected at most once.
CREATE UNIQUE INDEX IF NOT EXISTS idx_github_integrations_owner_repo
    ON github_integrations (owner, repo);

-- Operational index for status-filtered queries.
CREATE INDEX IF NOT EXISTS idx_github_integrations_status
    ON github_integrations (status);
