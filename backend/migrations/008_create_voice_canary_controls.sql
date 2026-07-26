-- Phase-1 Ella voice canary entitlement, enforcement, and content-free usage ledger.
--
-- Apply before enabling ELLA_VOICE_CANARY_ENFORCEMENT_ENABLED:
--   psql "$ELLA_POSTGRES_DSN" \
--     -f backend/migrations/008_create_voice_canary_controls.sql

CREATE TABLE IF NOT EXISTS voice_entitlements (
    uid TEXT COLLATE "C" PRIMARY KEY,
    status TEXT NOT NULL DEFAULT 'invited'
        CHECK (status IN ('invited', 'active', 'suspended', 'revoked', 'expired')),
    plan TEXT NOT NULL DEFAULT 'canary',
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    trial_started_at TIMESTAMPTZ,
    trial_expires_at TIMESTAMPTZ,
    daily_limit_s INTEGER NOT NULL DEFAULT 2700 CHECK (daily_limit_s > 0),
    monthly_limit_s INTEGER NOT NULL DEFAULT 43200 CHECK (monthly_limit_s > 0),
    daily_cost_limit_microusd BIGINT,
    monthly_cost_limit_microusd BIGINT,
    soft_limit_ratio NUMERIC(5, 4) NOT NULL DEFAULT 0.8
        CHECK (soft_limit_ratio > 0 AND soft_limit_ratio < 1),
    hard_limit_ratio NUMERIC(5, 4) NOT NULL DEFAULT 1.0
        CHECK (hard_limit_ratio >= 1),
    max_session_s INTEGER NOT NULL DEFAULT 1200 CHECK (max_session_s > 0),
    max_concurrent INTEGER NOT NULL DEFAULT 1 CHECK (max_concurrent > 0),
    max_audio_bytes_per_session BIGINT NOT NULL DEFAULT 120000000
        CHECK (max_audio_bytes_per_session > 0),
    max_audio_bytes_per_minute BIGINT NOT NULL DEFAULT 6000000
        CHECK (max_audio_bytes_per_minute > 0),
    provider_allowlist TEXT[] NOT NULL DEFAULT ARRAY['grok-voice'],
    model_allowlist TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    mode_allowlist TEXT[] NOT NULL DEFAULT ARRAY['v4'],
    fallback_policy JSONB NOT NULL DEFAULT '{"enabled": false, "order": []}'::jsonb,
    operator_note TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS voice_kill_switches (
    scope_type TEXT NOT NULL CHECK (scope_type IN ('global', 'user', 'provider')),
    scope_value TEXT COLLATE "C" NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT FALSE,
    reason TEXT,
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    updated_by TEXT NOT NULL DEFAULT 'operator',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (scope_type, scope_value)
);

CREATE TABLE IF NOT EXISTS voice_active_sessions (
    session_id TEXT COLLATE "C" PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL REFERENCES voice_entitlements(uid) ON DELETE CASCADE,
    correlation_id TEXT COLLATE "C" NOT NULL,
    entitlement_revision INTEGER NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL,
    accepted_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    input_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0 CHECK (input_audio_s >= 0),
    output_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0 CHECK (output_audio_s >= 0),
    input_audio_bytes BIGINT NOT NULL DEFAULT 0 CHECK (input_audio_bytes >= 0),
    output_audio_bytes BIGINT NOT NULL DEFAULT 0 CHECK (output_audio_bytes >= 0),
    tool_calls INTEGER NOT NULL DEFAULT 0 CHECK (tool_calls >= 0),
    reconnects INTEGER NOT NULL DEFAULT 0 CHECK (reconnects >= 0),
    provider_request_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    estimated_cost_microusd BIGINT NOT NULL DEFAULT 0
        CHECK (estimated_cost_microusd >= 0)
);

ALTER TABLE voice_active_sessions
    ADD COLUMN IF NOT EXISTS input_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0
        CHECK (input_audio_s >= 0),
    ADD COLUMN IF NOT EXISTS output_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0
        CHECK (output_audio_s >= 0),
    ADD COLUMN IF NOT EXISTS estimated_cost_microusd BIGINT NOT NULL DEFAULT 0
        CHECK (estimated_cost_microusd >= 0);

ALTER TABLE voice_entitlements
    ALTER COLUMN provider_allowlist SET DEFAULT ARRAY['grok-voice'],
    ALTER COLUMN mode_allowlist SET DEFAULT ARRAY['v4'],
    ALTER COLUMN fallback_policy SET DEFAULT '{"enabled": false, "order": []}'::jsonb;

CREATE INDEX IF NOT EXISTS voice_active_sessions_uid_idx
    ON voice_active_sessions (uid, last_seen_at DESC);

CREATE TABLE IF NOT EXISTS voice_rate_limit_events (
    id BIGSERIAL PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL,
    event_type TEXT NOT NULL CHECK (event_type IN ('token_issued', 'socket_accept', 'auth_failed')),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS voice_rate_limit_events_lookup_idx
    ON voice_rate_limit_events (uid, event_type, occurred_at DESC);

CREATE TABLE IF NOT EXISTS voice_usage_events (
    id UUID PRIMARY KEY,
    event_type TEXT NOT NULL CHECK (event_type IN (
        'token_issued',
        'session_accepted',
        'session_completed',
        'session_terminated',
        'policy_denied',
        'auth_failed',
        'alert'
    )),
    uid TEXT COLLATE "C" NOT NULL,
    session_id TEXT COLLATE "C",
    correlation_id TEXT COLLATE "C" NOT NULL,
    entitlement_revision INTEGER,
    provider TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    mode TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMPTZ,
    ended_at TIMESTAMPTZ,
    input_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0,
    output_audio_s NUMERIC(14, 3) NOT NULL DEFAULT 0,
    connection_s NUMERIC(14, 3) NOT NULL DEFAULT 0,
    input_audio_bytes BIGINT NOT NULL DEFAULT 0,
    output_audio_bytes BIGINT NOT NULL DEFAULT 0,
    tool_calls INTEGER NOT NULL DEFAULT 0,
    reconnects INTEGER NOT NULL DEFAULT 0,
    provider_request_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    termination_reason TEXT,
    normalized_error_code TEXT,
    estimated_cost_microusd BIGINT NOT NULL DEFAULT 0,
    reconciled_cost_microusd BIGINT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS voice_usage_events_uid_created_idx
    ON voice_usage_events (uid, created_at DESC);

CREATE INDEX IF NOT EXISTS voice_usage_events_rollup_idx
    ON voice_usage_events (uid, event_type, ended_at)
    WHERE event_type IN ('session_completed', 'session_terminated');

CREATE INDEX IF NOT EXISTS voice_usage_events_alert_idx
    ON voice_usage_events (event_type, normalized_error_code, created_at DESC)
    WHERE event_type IN ('auth_failed', 'alert');
