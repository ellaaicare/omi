-- Repair pre-ordinal diagnostic evidence written after migration 018.
--
-- Migration 019 added the relational column with a temporary zero default.
-- This migration gives every retained legacy attempt a deterministic ordinal
-- that preserves the old projection ordering and mirrors it into the JSONB
-- payload required by DiagnosticEventV1.

BEGIN;

DROP TRIGGER IF EXISTS ella_diagnostic_events_immutable ON ella_diagnostic_events;

WITH legacy_attempts AS MATERIALIZED (
    SELECT DISTINCT
        account_user_id,
        diagnostic_session_id,
        capture_attempt_id
    FROM ella_diagnostic_events
    WHERE NOT payload ? 'capture_attempt_ordinal'
),
attempt_candidates AS MATERIALIZED (
    SELECT DISTINCT ON (
        event.account_user_id,
        event.diagnostic_session_id,
        event.capture_attempt_id
    )
        event.account_user_id,
        event.diagnostic_session_id,
        event.capture_attempt_id,
        (event.event_name = 'capture_attempt_started') AS is_attempt_start,
        event.client_monotonic_ms,
        event.client_utc_time,
        event.client_sequence,
        event.event_id
    FROM ella_diagnostic_events event
    JOIN legacy_attempts legacy
      ON legacy.account_user_id = event.account_user_id
     AND legacy.diagnostic_session_id = event.diagnostic_session_id
     AND legacy.capture_attempt_id = event.capture_attempt_id
    ORDER BY
        event.account_user_id,
        event.diagnostic_session_id,
        event.capture_attempt_id,
        (event.event_name = 'capture_attempt_started') DESC,
        event.client_monotonic_ms DESC,
        event.client_utc_time DESC,
        event.client_sequence DESC,
        event.event_id DESC
),
ranked_attempts AS MATERIALIZED (
    SELECT
        account_user_id,
        diagnostic_session_id,
        capture_attempt_id,
        ROW_NUMBER() OVER (
            PARTITION BY account_user_id, diagnostic_session_id
            ORDER BY
                is_attempt_start,
                client_monotonic_ms,
                client_utc_time,
                client_sequence,
                event_id,
                capture_attempt_id
        ) - 1 AS capture_attempt_ordinal
    FROM attempt_candidates
)
UPDATE ella_diagnostic_events event
SET capture_attempt_ordinal = ranked.capture_attempt_ordinal,
    payload = jsonb_set(
        event.payload,
        '{capture_attempt_ordinal}',
        to_jsonb(ranked.capture_attempt_ordinal),
        TRUE
    )
FROM ranked_attempts ranked
WHERE event.account_user_id = ranked.account_user_id
  AND event.diagnostic_session_id = ranked.diagnostic_session_id
  AND event.capture_attempt_id = ranked.capture_attempt_id
  AND NOT event.payload ? 'capture_attempt_ordinal';

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ella_diagnostic_events
        GROUP BY account_user_id, diagnostic_session_id, capture_attempt_id
        HAVING COUNT(DISTINCT capture_attempt_ordinal) != 1
    ) OR EXISTS (
        SELECT 1
        FROM ella_diagnostic_events
        GROUP BY account_user_id, diagnostic_session_id, capture_attempt_ordinal
        HAVING COUNT(DISTINCT capture_attempt_id) != 1
    ) THEN
        RAISE EXCEPTION 'diagnostic attempt ordinal mapping is not one-to-one';
    END IF;
END;
$$;

CREATE TRIGGER ella_diagnostic_events_immutable
    BEFORE UPDATE ON ella_diagnostic_events
    FOR EACH ROW EXECUTE FUNCTION ella_reject_diagnostic_event_update();

COMMIT;
