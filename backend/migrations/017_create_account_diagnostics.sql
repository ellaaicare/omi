-- Account-scoped, content-free diagnostic evidence for Ella support.
--
-- Diagnostic rows are immutable evidence, not product-state authority. Deletes
-- remain available only for bounded retention and account-deletion cascades.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ella_diagnostic_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    profile_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    event_id TEXT COLLATE "C" NOT NULL
        CHECK (event_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    diagnostic_session_id TEXT COLLATE "C" NOT NULL
        CHECK (diagnostic_session_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    capture_attempt_id TEXT COLLATE "C" NOT NULL
        CHECK (capture_attempt_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    account_binding_fingerprint CHAR(64) COLLATE "C" NOT NULL
        CHECK (account_binding_fingerprint ~ '^[0-9a-f]{64}$'),
    authority_generation BIGINT NOT NULL CHECK (authority_generation >= 0),
    layer TEXT COLLATE "C" NOT NULL CHECK (
        layer IN (
            'account_binding', 'ble_transport', 'physical_audio',
            'server_capture', 'publication', 'presentation'
        )
    ),
    event_name TEXT COLLATE "C" NOT NULL
        CHECK (event_name ~ '^[a-z][a-z0-9_]{0,63}$'),
    outcome TEXT COLLATE "C" NOT NULL
        CHECK (outcome IN ('started', 'succeeded', 'failed', 'cancelled', 'unknown')),
    stable_failure_code TEXT COLLATE "C",
    client_sequence BIGINT NOT NULL CHECK (client_sequence >= 0),
    client_monotonic_ms BIGINT NOT NULL CHECK (client_monotonic_ms >= 0),
    client_utc_time TIMESTAMPTZ NOT NULL,
    payload JSONB NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    server_received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (CURRENT_TIMESTAMP + INTERVAL '30 days'),
    UNIQUE (account_user_id, event_id),
    UNIQUE (account_user_id, diagnostic_session_id, capture_attempt_id, client_sequence),
    CHECK (expires_at > server_received_at)
);

CREATE INDEX IF NOT EXISTS ella_diagnostic_events_session_idx
    ON ella_diagnostic_events (
        account_user_id, diagnostic_session_id, server_received_at, client_sequence
    );

CREATE INDEX IF NOT EXISTS ella_diagnostic_events_attempt_start_idx
    ON ella_diagnostic_events (
        account_user_id, profile_user_id, diagnostic_session_id,
        client_monotonic_ms DESC, client_utc_time DESC,
        client_sequence DESC, event_id DESC
    )
    WHERE event_name = 'capture_attempt_started';

CREATE INDEX IF NOT EXISTS ella_diagnostic_events_retention_idx
    ON ella_diagnostic_events (expires_at);

CREATE OR REPLACE FUNCTION ella_reject_diagnostic_event_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ella_diagnostic_events are immutable';
END;
$$;

DROP TRIGGER IF EXISTS ella_diagnostic_events_immutable ON ella_diagnostic_events;
CREATE TRIGGER ella_diagnostic_events_immutable
    BEFORE UPDATE ON ella_diagnostic_events
    FOR EACH ROW EXECUTE FUNCTION ella_reject_diagnostic_event_update();

CREATE TABLE IF NOT EXISTS ella_diagnostic_support_grants (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    profile_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    diagnostic_session_id TEXT COLLATE "C" NOT NULL
        CHECK (diagnostic_session_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    code_hash CHAR(64) COLLATE "C" NOT NULL UNIQUE
        CHECK (code_hash ~ '^[0-9a-f]{64}$'),
    evidence_not_before TIMESTAMPTZ NOT NULL,
    evidence_not_after TIMESTAMPTZ NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    redeemed_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (evidence_not_before < evidence_not_after),
    CHECK (expires_at <= created_at + INTERVAL '15 minutes'),
    CHECK (redeemed_at IS NULL OR revoked_at IS NULL)
);

CREATE INDEX IF NOT EXISTS ella_diagnostic_support_grants_owner_idx
    ON ella_diagnostic_support_grants (account_user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS ella_diagnostic_support_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    grant_id UUID NOT NULL
        REFERENCES ella_diagnostic_support_grants(id) ON DELETE CASCADE ON UPDATE CASCADE,
    account_user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    operator_id TEXT COLLATE "C" NOT NULL
        CHECK (operator_id ~ '^[A-Za-z0-9][A-Za-z0-9._@-]{0,127}$'),
    case_id TEXT COLLATE "C" NOT NULL
        CHECK (case_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$'),
    reason TEXT COLLATE "C" NOT NULL
        CHECK (reason ~ '^[a-z][a-z0-9_]{2,63}$'),
    action TEXT COLLATE "C" NOT NULL CHECK (action IN ('support_projection_read')),
    observed_event_count INTEGER NOT NULL CHECK (observed_event_count >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS ella_diagnostic_support_audit_case_idx
    ON ella_diagnostic_support_audit (case_id, created_at DESC);

COMMIT;
