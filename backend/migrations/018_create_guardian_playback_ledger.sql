-- Durable owner-bound Guardian playback ledger.
--
-- Replaces the in-memory `_playback_events` dict in
-- ella/routers/guardian.py, which reset on every restart and could not be
-- read from the scanner process without importing the router module. This
-- table is the single source of truth for "did this owner's device actually
-- play this Whisper back" across the enqueue -> next-audio -> iOS playback
-- receipt lifecycle.
--
-- Lifecycle: generated -> queued -> fetched -> started -> completed|failed.
-- Only an authenticated started/completed receipt from the owner's own
-- device is evidence that audio actually played out loud. generated,
-- queued and fetched rows exist so the pipeline can reason about latency
-- and drop-off, but MUST NOT be treated as playback by any caller.
--
-- Retention: rows are owner-scoped operational telemetry, not conversation
-- content the product retains indefinitely. Default retention is 7 days
-- (ELLA_GUARDIAN_PLAYBACK_LEDGER_RETENTION_DAYS), enforced by
-- guardian_playback_ledger.cleanup_expired() rather than by this migration,
-- so the retention window can change without a schema change.

BEGIN;

CREATE TABLE IF NOT EXISTS guardian_playback_ledger (
    id BIGSERIAL PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL,
    playback_id TEXT COLLATE "C" NOT NULL,
    queue_item_id TEXT COLLATE "C",
    audio_id TEXT COLLATE "C",
    trace_id TEXT COLLATE "C",
    status TEXT COLLATE "C" NOT NULL DEFAULT 'generated'
        CHECK (status IN ('generated', 'queued', 'fetched', 'started', 'completed', 'failed')),
    purpose TEXT COLLATE "C",
    playback_text TEXT,
    text_provenance TEXT COLLATE "C",
    route TEXT COLLATE "C",
    device_class TEXT COLLATE "C",
    port_name TEXT COLLATE "C",
    device_uid TEXT COLLATE "C",
    duration_ms INTEGER,
    error_message TEXT,
    generated_at TIMESTAMPTZ,
    queued_at TIMESTAMPTZ,
    fetched_at TIMESTAMPTZ,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    failed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (uid, playback_id)
);

-- Owner-scoped candidate lookup for the scanner: "what did this uid's device
-- actually play recently" (started/completed only — enforced in application
-- code, this index just makes that query cheap).
CREATE INDEX IF NOT EXISTS guardian_playback_ledger_uid_started_idx
    ON guardian_playback_ledger (uid, started_at DESC)
    WHERE started_at IS NOT NULL;

CREATE INDEX IF NOT EXISTS guardian_playback_ledger_uid_created_idx
    ON guardian_playback_ledger (uid, created_at DESC);

CREATE INDEX IF NOT EXISTS guardian_playback_ledger_trace_idx
    ON guardian_playback_ledger (trace_id)
    WHERE trace_id IS NOT NULL;

-- Retention cleanup scans by created_at only (no uid), so keep it a plain
-- btree rather than owner-scoped.
CREATE INDEX IF NOT EXISTS guardian_playback_ledger_created_at_idx
    ON guardian_playback_ledger (created_at);

COMMIT;
