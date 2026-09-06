-- Give each capture attempt a restart-safe order within its diagnostic session.

BEGIN;

ALTER TABLE ella_diagnostic_events
    ADD COLUMN IF NOT EXISTS capture_attempt_ordinal BIGINT NOT NULL DEFAULT 0
        CHECK (capture_attempt_ordinal >= 0);

-- The temporary default makes this migration safe for any pre-rollout rows.
-- V1 writers must always provide the ordinal after the migration completes.
ALTER TABLE ella_diagnostic_events
    ALTER COLUMN capture_attempt_ordinal DROP DEFAULT;

CREATE INDEX IF NOT EXISTS ella_diagnostic_events_attempt_ordinal_idx
    ON ella_diagnostic_events (
        account_user_id, profile_user_id, diagnostic_session_id,
        capture_attempt_ordinal DESC, client_utc_time DESC,
        client_monotonic_ms DESC, client_sequence DESC, event_id DESC
    );

COMMIT;
