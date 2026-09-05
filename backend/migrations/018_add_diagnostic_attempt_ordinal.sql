-- Give each capture attempt a restart-safe order within its diagnostic session.

BEGIN;

ALTER TABLE ella_diagnostic_events
    ADD COLUMN IF NOT EXISTS capture_attempt_ordinal BIGINT NOT NULL DEFAULT 0
        CHECK (capture_attempt_ordinal >= 0);

-- Migration 017 made the evidence table update-immutable. Temporarily remove
-- that guard inside this transaction so retained payloads remain readable by
-- the now-required v1 field; a failed migration rolls the trigger drop back.
DROP TRIGGER IF EXISTS ella_diagnostic_events_immutable ON ella_diagnostic_events;

UPDATE ella_diagnostic_events
SET payload = jsonb_set(
    payload,
    '{capture_attempt_ordinal}',
    to_jsonb(capture_attempt_ordinal),
    TRUE
)
WHERE NOT payload ? 'capture_attempt_ordinal';

CREATE TRIGGER ella_diagnostic_events_immutable
    BEFORE UPDATE ON ella_diagnostic_events
    FOR EACH ROW EXECUTE FUNCTION ella_reject_diagnostic_event_update();

-- The temporary default makes the relational and JSON backfill safe for any
-- retained rows. V1 writers must always provide the ordinal afterward.
ALTER TABLE ella_diagnostic_events
    ALTER COLUMN capture_attempt_ordinal DROP DEFAULT;

CREATE INDEX IF NOT EXISTS ella_diagnostic_events_attempt_ordinal_idx
    ON ella_diagnostic_events (
        account_user_id, profile_user_id, diagnostic_session_id,
        capture_attempt_ordinal DESC, client_utc_time DESC,
        client_monotonic_ms DESC, client_sequence DESC, event_id DESC
    );

COMMIT;
