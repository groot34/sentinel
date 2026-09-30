-- Transactional outbox table for reliable event delivery
-- Mission 11: Transactional Outbox & Event Delivery

CREATE TABLE IF NOT EXISTS event_outbox (
    event_id VARCHAR(64) PRIMARY KEY,
    investigation_id VARCHAR(128) NOT NULL,
    event_type VARCHAR(64) NOT NULL,
    payload JSONB NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'pending',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    processed_at TIMESTAMP WITH TIME ZONE,
    retry_count INTEGER NOT NULL DEFAULT 0,
    error_message TEXT
);

-- Indexes for efficient polling and querying
CREATE INDEX IF NOT EXISTS idx_event_outbox_status_created ON event_outbox (status, created_at);
CREATE INDEX IF NOT EXISTS idx_event_outbox_investigation_id ON event_outbox (investigation_id);
CREATE INDEX IF NOT EXISTS idx_event_outbox_event_type ON event_outbox (event_type);

-- Comment explaining the table purpose
COMMENT ON TABLE event_outbox IS 'Transactional outbox for durable domain events. Events are inserted atomically with state changes and delivered by a background worker.';
