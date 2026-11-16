-- 004_approval_snapshots
--
-- Immutable evidence snapshots captured at the moment a human records an
-- approval or rejection decision for a fix proposal (Mission 26.2).
--
-- Design notes:
--   * One row per (investigation_id, proposal_id). The existing approval
--     domain keeps at most one decision per proposal (state.update_approval
--     replaces the record in place), so the unique index is the correct
--     cardinality and also makes duplicate submissions a no-op.
--   * Rows are write-once: application code never UPDATEs or DELETEs this
--     table, so a later investigation update can never rewrite history.
--   * integrity_hash is the SHA-256 digest (64 hex chars) of a canonical
--     JSON serialization of snapshot_payload. It is an audit aid, not an
--     access-control mechanism.
--   * No foreign key is declared, matching the existing event_outbox table,
--     so deleting an investigation does not cascade into audit history.

CREATE TABLE IF NOT EXISTS approval_snapshots (
    snapshot_id BIGSERIAL PRIMARY KEY,
    investigation_id VARCHAR(128) NOT NULL,
    proposal_id VARCHAR(32) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    reviewer VARCHAR(128) NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL,
    snapshot_version INTEGER NOT NULL,
    integrity_hash VARCHAR(64) NOT NULL,
    snapshot_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT approval_snapshots_decision_valid
        CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT approval_snapshots_snapshot_version_positive
        CHECK (snapshot_version >= 1),
    CONSTRAINT approval_snapshots_integrity_hash_length
        CHECK (char_length(integrity_hash) = 64)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approval_snapshots_investigation_proposal
    ON approval_snapshots (investigation_id, proposal_id);

CREATE INDEX IF NOT EXISTS idx_approval_snapshots_investigation
    ON approval_snapshots (investigation_id);

CREATE INDEX IF NOT EXISTS idx_approval_snapshots_decided_at
    ON approval_snapshots (decided_at);
