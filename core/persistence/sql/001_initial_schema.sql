-- Sentinel 2.0 - Mission 05: PostgreSQL Initial Schema
-- Single table: investigations

CREATE TABLE IF NOT EXISTS investigations (
    investigation_id VARCHAR(128) PRIMARY KEY,
    status VARCHAR(32) NOT NULL,
    current_stage VARCHAR(64),
    version INTEGER NOT NULL DEFAULT 1,
    llm_call_count INTEGER NOT NULL DEFAULT 0,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ,
    error TEXT,
    state_payload JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Operational indexes for dashboard and querying
CREATE INDEX IF NOT EXISTS idx_investigations_status ON investigations(status);
CREATE INDEX IF NOT EXISTS idx_investigations_current_stage ON investigations(current_stage);
CREATE INDEX IF NOT EXISTS idx_investigations_started_at ON investigations(started_at DESC);
