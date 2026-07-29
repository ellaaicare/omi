-- Ella invite redemption, capacity reservations, privacy-safe audit receipts,
-- and bounded abuse controls.
--
-- This migration intentionally stores invitation codes, Firebase UID audit
-- references, and source-address references only as domain-separated HMACs.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ella_invitation_capacity_reservations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    pool_key TEXT COLLATE "C" NOT NULL
        CHECK (pool_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    state TEXT NOT NULL DEFAULT 'reserved'
        CHECK (state IN ('reserved', 'consumed', 'released', 'expired')),
    reserved_slots INTEGER NOT NULL DEFAULT 1 CHECK (reserved_slots > 0),
    consumed_slots INTEGER NOT NULL DEFAULT 0
        CHECK (consumed_slots >= 0 AND consumed_slots <= reserved_slots),
    expires_at TIMESTAMPTZ,
    consumed_at TIMESTAMPTZ,
    released_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ella_invitation_capacity_pool_state_idx
    ON ella_invitation_capacity_reservations (pool_key, state, expires_at);

CREATE TABLE IF NOT EXISTS ella_invitations (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    capacity_reservation_id UUID NOT NULL
        REFERENCES ella_invitation_capacity_reservations(id) ON DELETE RESTRICT,
    kind TEXT NOT NULL CHECK (kind IN ('ordinary', 'app_review')),
    code_hmac CHAR(64) COLLATE "C" NOT NULL UNIQUE
        CHECK (code_hmac ~ '^[0-9a-f]{64}$'),
    display_hint VARCHAR(2)
        CHECK (display_hint IS NULL OR display_hint ~ '^[A-HJ-KM-NP-Z2-9]{2}$'),
    state TEXT NOT NULL DEFAULT 'issued'
        CHECK (state IN ('issued', 'sent', 'redeemed', 'revoked', 'expired')),
    delivery_state TEXT NOT NULL DEFAULT 'pending'
        CHECK (delivery_state IN ('pending', 'sent', 'failed', 'suppressed')),
    usage_mode TEXT NOT NULL DEFAULT 'single_use'
        CHECK (usage_mode IN ('single_use', 'capped_multi_redeem')),
    max_redemptions INTEGER NOT NULL DEFAULT 1 CHECK (max_redemptions > 0),
    redemption_count INTEGER NOT NULL DEFAULT 0
        CHECK (redemption_count >= 0 AND redemption_count <= max_redemptions),
    reserved_setup_slots INTEGER NOT NULL DEFAULT 1
        CHECK (reserved_setup_slots > 0),
    entitlement_policy_revision TEXT COLLATE "C" NOT NULL
        CHECK (entitlement_policy_revision ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    entitlement_policy JSONB NOT NULL,
    required_consent_policy_version TEXT COLLATE "C" NOT NULL,
    required_consent_processor_set_hash TEXT COLLATE "C" NOT NULL
        CHECK (required_consent_processor_set_hash ~ '^sha256:[0-9a-f]{64}$'),
    required_consent_scope_version TEXT COLLATE "C" NOT NULL,
    required_consent_scope_hash TEXT COLLATE "C" NOT NULL
        CHECK (required_consent_scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    cohort TEXT COLLATE "C" NOT NULL DEFAULT 'founding_family'
        CHECK (cohort ~ '^[a-z0-9][a-z0-9._-]{0,63}$'),
    exclude_from_product_analytics BOOLEAN NOT NULL DEFAULT FALSE,
    issued_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    first_sent_at TIMESTAMPTZ,
    expires_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT ella_invitations_kind_policy_check CHECK (
        (
            kind = 'ordinary'
            AND usage_mode = 'single_use'
            AND max_redemptions = 1
            AND reserved_setup_slots = 1
            AND (
                state = 'issued'
                OR (first_sent_at IS NOT NULL AND expires_at IS NOT NULL)
            )
        )
        OR (
            kind = 'app_review'
            AND usage_mode = 'capped_multi_redeem'
            AND max_redemptions <= 20
            AND reserved_setup_slots = 2
            AND cohort = 'app_review'
            AND exclude_from_product_analytics = TRUE
            AND expires_at IS NULL
        )
    )
);

CREATE INDEX IF NOT EXISTS ella_invitations_state_expiry_idx
    ON ella_invitations (state, expires_at);

ALTER TABLE voice_entitlements
    ADD COLUMN IF NOT EXISTS invitation_id UUID,
    ADD COLUMN IF NOT EXISTS entitlement_policy_revision TEXT COLLATE "C",
    ADD COLUMN IF NOT EXISTS cohort TEXT COLLATE "C" NOT NULL DEFAULT 'canary',
    ADD COLUMN IF NOT EXISTS exclude_from_product_analytics BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS consent_policy_version TEXT COLLATE "C",
    ADD COLUMN IF NOT EXISTS consent_processor_set_hash TEXT COLLATE "C",
    ADD COLUMN IF NOT EXISTS consent_scope_version TEXT COLLATE "C",
    ADD COLUMN IF NOT EXISTS consent_scope_hash TEXT COLLATE "C";

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'voice_entitlements_invitation_id_fkey'
    ) THEN
        ALTER TABLE voice_entitlements
            ADD CONSTRAINT voice_entitlements_invitation_id_fkey
            FOREIGN KEY (invitation_id) REFERENCES ella_invitations(id)
            ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE connamespace = current_schema()::regnamespace
          AND conname = 'voice_entitlements_invitation_consent_lineage_check'
    ) THEN
        ALTER TABLE voice_entitlements
            ADD CONSTRAINT voice_entitlements_invitation_consent_lineage_check
            CHECK (
                invitation_id IS NULL
                OR (
                    consent_policy_version IS NOT NULL
                    AND consent_processor_set_hash ~ '^sha256:[0-9a-f]{64}$'
                    AND consent_scope_version IS NOT NULL
                    AND consent_scope_hash ~ '^sha256:[0-9a-f]{64}$'
                )
            ) NOT VALID;
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS voice_entitlements_invitation_idx
    ON voice_entitlements (invitation_id)
    WHERE invitation_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS ella_invitation_targets (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invitation_id UUID NOT NULL
        REFERENCES ella_invitations(id) ON DELETE RESTRICT,
    account_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (account_ref_hmac ~ '^[0-9a-f]{64}$'),
    profile_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (profile_ref_hmac ~ '^[0-9a-f]{64}$'),
    required_profile_class TEXT NOT NULL DEFAULT 'synthetic'
        CHECK (required_profile_class = 'synthetic'),
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ella_invitation_targets_exact_key
    ON ella_invitation_targets (
        invitation_id, account_ref_hmac, profile_ref_hmac
    );

CREATE TABLE IF NOT EXISTS ella_invitation_redemptions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invitation_id UUID NOT NULL REFERENCES ella_invitations(id) ON DELETE RESTRICT,
    invitation_target_id UUID NOT NULL
        REFERENCES ella_invitation_targets(id) ON DELETE RESTRICT,
    uid_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (uid_ref_hmac ~ '^[0-9a-f]{64}$'),
    consent_receipt_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (consent_receipt_ref_hmac ~ '^[0-9a-f]{64}$'),
    entitlement_revision INTEGER NOT NULL CHECK (entitlement_revision >= 1),
    support_code TEXT COLLATE "C" NOT NULL,
    correlation_id UUID NOT NULL,
    app_build TEXT,
    outcome TEXT NOT NULL DEFAULT 'redeemed' CHECK (outcome = 'redeemed'),
    redeemed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE UNIQUE INDEX IF NOT EXISTS ella_invitation_redemptions_invite_uid_key
    ON ella_invitation_redemptions (invitation_id, uid_ref_hmac);
CREATE UNIQUE INDEX IF NOT EXISTS ella_invitation_redemptions_target_key
    ON ella_invitation_redemptions (invitation_target_id);
CREATE INDEX IF NOT EXISTS ella_invitation_redemptions_invite_time_idx
    ON ella_invitation_redemptions (invitation_id, redeemed_at);

CREATE TABLE IF NOT EXISTS ella_invitation_audit_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    invitation_id UUID REFERENCES ella_invitations(id) ON DELETE RESTRICT,
    uid_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (uid_ref_hmac ~ '^[0-9a-f]{64}$'),
    source_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (source_ref_hmac ~ '^[0-9a-f]{64}$'),
    event_type TEXT NOT NULL CHECK (event_type IN (
        'redeemed', 'idempotent_retry', 'invalid', 'expired', 'capacity',
        'rate_limited', 'policy_invalid', 'redemption_disabled'
    )),
    support_code TEXT COLLATE "C" NOT NULL,
    correlation_id UUID NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ella_invitation_audit_uid_time_idx
    ON ella_invitation_audit_receipts (uid_ref_hmac, created_at DESC);
CREATE INDEX IF NOT EXISTS ella_invitation_audit_source_time_idx
    ON ella_invitation_audit_receipts (source_ref_hmac, created_at DESC);

CREATE TABLE IF NOT EXISTS ella_invitation_rate_limit_events (
    id BIGSERIAL PRIMARY KEY,
    uid_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (uid_ref_hmac ~ '^[0-9a-f]{64}$'),
    source_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (source_ref_hmac ~ '^[0-9a-f]{64}$'),
    failure_code TEXT NOT NULL CHECK (failure_code IN (
        'invalid', 'expired', 'capacity'
    )),
    occurred_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS ella_invitation_rate_uid_time_idx
    ON ella_invitation_rate_limit_events (uid_ref_hmac, occurred_at DESC);
CREATE INDEX IF NOT EXISTS ella_invitation_rate_source_time_idx
    ON ella_invitation_rate_limit_events (source_ref_hmac, occurred_at DESC);

CREATE TABLE IF NOT EXISTS ella_invitation_security_alerts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    alert_type TEXT NOT NULL CHECK (alert_type = 'redemption_anomaly'),
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'resolved')),
    window_started_at TIMESTAMPTZ NOT NULL,
    failure_count INTEGER NOT NULL CHECK (failure_count > 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    delivered_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS ella_invitation_security_one_pending_idx
    ON ella_invitation_security_alerts (alert_type)
    WHERE state = 'pending';

COMMIT;
