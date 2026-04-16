-- Guardian delivery dispatch idempotency and audit log.
--
-- Apply against the Ella Postgres database:
--   psql "$ELLA_POSTGRES_DSN" -f backend/migrations/006_create_guardian_delivery_log.sql

CREATE TABLE IF NOT EXISTS guardian_delivery_log (
    id BIGSERIAL PRIMARY KEY,
    trace_id TEXT NOT NULL,
    uid TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL,
    target TEXT NOT NULL,
    caregiver_id TEXT,
    recipient_phone TEXT,
    recipient_email TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    provider_response JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE guardian_delivery_log
    ADD COLUMN IF NOT EXISTS caregiver_id TEXT,
    ADD COLUMN IF NOT EXISTS recipient_phone TEXT,
    ADD COLUMN IF NOT EXISTS recipient_email TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS guardian_delivery_log_trace_channel_target_uidx
    ON guardian_delivery_log (trace_id, channel, target);

CREATE INDEX IF NOT EXISTS guardian_delivery_log_uid_created_idx
    ON guardian_delivery_log (uid, created_at DESC);

CREATE INDEX IF NOT EXISTS guardian_delivery_log_status_updated_idx
    ON guardian_delivery_log (status, updated_at DESC);
