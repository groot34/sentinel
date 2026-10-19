-- Concurrent and crash-safe outbox delivery (Mission 16).
--
-- Adds durable delivery-claim state to the transactional outbox so that
-- multiple worker processes can claim disjoint event batches atomically
-- (FOR UPDATE SKIP LOCKED) and so that retries are scheduled durably rather
-- than by sleeping inside the delivery loop.
--
-- Additive and idempotent: safe to run on a legacy 002-era outbox as well as
-- on a freshly created one. Existing rows become immediately eligible because
-- next_attempt_at defaults to NOW().

ALTER TABLE event_outbox
    ADD COLUMN IF NOT EXISTS claimed_by VARCHAR(128),
    ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS claim_expires_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW();

-- Supports the claim query: eligible pending events ordered by
-- (created_at ASC, event_id ASC), and claim-expiry recovery sweeps.
CREATE INDEX IF NOT EXISTS idx_event_outbox_claim
    ON event_outbox (status, next_attempt_at, created_at);

COMMENT ON COLUMN event_outbox.claimed_by IS 'Worker identity holding the current delivery claim, or NULL when unclaimed.';
COMMENT ON COLUMN event_outbox.claimed_at IS 'When the current delivery claim was taken, or NULL when unclaimed.';
COMMENT ON COLUMN event_outbox.claim_expires_at IS 'Visibility timeout for the current claim; an expired claim may be recovered by another worker.';
COMMENT ON COLUMN event_outbox.next_attempt_at IS 'Earliest time this event may be claimed for delivery (durable retry scheduling).';
