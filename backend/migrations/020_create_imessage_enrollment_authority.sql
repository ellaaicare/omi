-- Owner-scoped iMessage text-DM consent and enrollment authority.
--
-- This lane is intentionally separate from migration 009's one-owner Hermes
-- Cloud Photon canary. Registration proves transport reachability only; an
-- inbound handset proof and current owner/runtime/consent authority are all
-- required before a binding can become active.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE ella_imessage_consent_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    request_id UUID NOT NULL,
    decision TEXT COLLATE "C" NOT NULL CHECK (decision IN ('granted', 'declined', 'revoked')),
    policy_version TEXT COLLATE "C" NOT NULL,
    processor_set_hash TEXT COLLATE "C" NOT NULL CHECK (processor_set_hash ~ '^sha256:[0-9a-f]{64}$'),
    scope_version TEXT COLLATE "C" NOT NULL,
    scope_hash TEXT COLLATE "C" NOT NULL CHECK (scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    authority_epoch UUID NOT NULL,
    authority_revision INTEGER NOT NULL CHECK (authority_revision >= 1),
    app_version TEXT COLLATE "C" NOT NULL,
    build_number TEXT COLLATE "C" NOT NULL,
    decided_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (user_id, request_id),
    UNIQUE (user_id, authority_revision),
    UNIQUE (user_id, id)
);

CREATE TABLE ella_imessage_consent_authority (
    user_id UUID PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    current_receipt_id UUID NOT NULL,
    decision TEXT COLLATE "C" NOT NULL CHECK (decision IN ('granted', 'declined', 'revoked')),
    policy_version TEXT COLLATE "C" NOT NULL,
    processor_set_hash TEXT COLLATE "C" NOT NULL CHECK (processor_set_hash ~ '^sha256:[0-9a-f]{64}$'),
    scope_version TEXT COLLATE "C" NOT NULL,
    scope_hash TEXT COLLATE "C" NOT NULL CHECK (scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    authority_epoch UUID NOT NULL,
    revision INTEGER NOT NULL CHECK (revision >= 1),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_imessage_consent_authority_receipt_fkey
        FOREIGN KEY (user_id, current_receipt_id)
        REFERENCES ella_imessage_consent_receipts(user_id, id)
        ON DELETE RESTRICT ON UPDATE CASCADE
);

CREATE TABLE ella_imessage_registration_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    idempotency_key UUID NOT NULL,
    handset_ref_hmac CHAR(64) COLLATE "C" NOT NULL CHECK (handset_ref_hmac ~ '^[0-9a-f]{64}$'),
    consent_receipt_id UUID NOT NULL,
    consent_authority_epoch UUID NOT NULL,
    runtime_binding_id UUID NOT NULL REFERENCES ella_runtime_bindings(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_target_id UUID NOT NULL REFERENCES ella_runtime_targets(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_authority_digest CHAR(64) COLLATE "C" NOT NULL CHECK (runtime_authority_digest ~ '^[0-9a-f]{64}$'),
    state TEXT COLLATE "C" NOT NULL DEFAULT 'prepared'
        CHECK (state IN ('prepared', 'provider_accepted', 'finalized', 'failed', 'uncertain', 'quarantined')),
    provider_request_id UUID NOT NULL,
    provider_registration_ref_hmac CHAR(64) COLLATE "C"
        CHECK (provider_registration_ref_hmac IS NULL OR provider_registration_ref_hmac ~ '^[0-9a-f]{64}$'),
    assigned_destination_e164 TEXT COLLATE "C"
        CHECK (assigned_destination_e164 IS NULL OR assigned_destination_e164 ~ '^[+][1-9][0-9]{7,14}$'),
    assigned_destination_ref_hmac CHAR(64) COLLATE "C"
        CHECK (assigned_destination_ref_hmac IS NULL OR assigned_destination_ref_hmac ~ '^[0-9a-f]{64}$'),
    error_code TEXT COLLATE "C",
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_imessage_registration_attempts_consent_fkey
        FOREIGN KEY (user_id, consent_receipt_id)
        REFERENCES ella_imessage_consent_receipts(user_id, id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    UNIQUE (user_id, idempotency_key),
    UNIQUE (provider_request_id),
    UNIQUE (user_id, id)
);

ALTER TABLE ella_imessage_registration_attempts
    ADD CONSTRAINT ella_imessage_registration_attempts_provider_shape CHECK (
        (
            state = 'prepared'
            AND provider_registration_ref_hmac IS NULL
            AND assigned_destination_e164 IS NULL
            AND assigned_destination_ref_hmac IS NULL
        )
        OR (
            state IN ('provider_accepted', 'finalized', 'quarantined')
            AND provider_registration_ref_hmac IS NOT NULL
            AND assigned_destination_e164 IS NOT NULL
            AND assigned_destination_ref_hmac IS NOT NULL
        )
        OR (
            state = 'uncertain'
            AND (
                (
                    provider_registration_ref_hmac IS NULL
                    AND assigned_destination_e164 IS NULL
                    AND assigned_destination_ref_hmac IS NULL
                )
                OR (
                    provider_registration_ref_hmac IS NOT NULL
                    AND assigned_destination_e164 IS NOT NULL
                    AND assigned_destination_ref_hmac IS NOT NULL
                )
            )
        )
        OR (
            state = 'failed'
            AND provider_registration_ref_hmac IS NULL
            AND assigned_destination_e164 IS NULL
            AND assigned_destination_ref_hmac IS NULL
        )
    );

CREATE UNIQUE INDEX ella_imessage_registration_attempts_provider_registration_key
    ON ella_imessage_registration_attempts(provider_registration_ref_hmac)
    WHERE provider_registration_ref_hmac IS NOT NULL;

CREATE TABLE ella_imessage_channel_bindings (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    registration_attempt_id UUID NOT NULL,
    provider TEXT COLLATE "C" NOT NULL DEFAULT 'photon' CHECK (provider = 'photon'),
    role TEXT COLLATE "C" NOT NULL DEFAULT 'user' CHECK (role = 'user'),
    status TEXT COLLATE "C" NOT NULL DEFAULT 'verification_pending'
        CHECK (status IN ('verification_pending', 'active', 'revoked', 'quarantined')),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
    handset_ref_hmac CHAR(64) COLLATE "C" NOT NULL CHECK (handset_ref_hmac ~ '^[0-9a-f]{64}$'),
    assigned_destination_e164 TEXT COLLATE "C" NOT NULL
        CHECK (assigned_destination_e164 ~ '^[+][1-9][0-9]{7,14}$'),
    assigned_destination_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (assigned_destination_ref_hmac ~ '^[0-9a-f]{64}$'),
    line_identity_hmac CHAR(64) COLLATE "C" CHECK (line_identity_hmac IS NULL OR line_identity_hmac ~ '^[0-9a-f]{64}$'),
    contact_identity_hmac CHAR(64) COLLATE "C"
        CHECK (contact_identity_hmac IS NULL OR contact_identity_hmac ~ '^[0-9a-f]{64}$'),
    provider_registration_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (provider_registration_ref_hmac ~ '^[0-9a-f]{64}$'),
    runtime_binding_id UUID NOT NULL REFERENCES ella_runtime_bindings(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_target_id UUID NOT NULL REFERENCES ella_runtime_targets(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_authority_digest CHAR(64) COLLATE "C" NOT NULL CHECK (runtime_authority_digest ~ '^[0-9a-f]{64}$'),
    consent_receipt_id UUID NOT NULL,
    consent_authority_epoch UUID NOT NULL,
    challenge_salt CHAR(32) COLLATE "C" NOT NULL CHECK (challenge_salt ~ '^[0-9a-f]{32}$'),
    challenge_hash CHAR(64) COLLATE "C" NOT NULL CHECK (challenge_hash ~ '^[0-9a-f]{64}$'),
    challenge_expires_at TIMESTAMPTZ NOT NULL,
    challenge_attempts INTEGER NOT NULL DEFAULT 0 CHECK (challenge_attempts BETWEEN 0 AND 5),
    verified_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    last_revoke_idempotency_key UUID,
    last_transport_healthy_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_imessage_channel_bindings_attempt_fkey
        FOREIGN KEY (user_id, registration_attempt_id)
        REFERENCES ella_imessage_registration_attempts(user_id, id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT ella_imessage_channel_bindings_consent_fkey
        FOREIGN KEY (user_id, consent_receipt_id)
        REFERENCES ella_imessage_consent_receipts(user_id, id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT ella_imessage_channel_bindings_verified_shape CHECK (
        (status = 'active' AND verified_at IS NOT NULL AND line_identity_hmac IS NOT NULL AND contact_identity_hmac IS NOT NULL)
        OR status <> 'active'
    ),
    UNIQUE (registration_attempt_id)
);

CREATE UNIQUE INDEX ella_imessage_channel_bindings_owner_current_key
    ON ella_imessage_channel_bindings(user_id, role)
    WHERE status IN ('verification_pending', 'active');

CREATE UNIQUE INDEX ella_imessage_channel_bindings_pending_destination_handset_key
    ON ella_imessage_channel_bindings(assigned_destination_ref_hmac, handset_ref_hmac)
    WHERE status IN ('verification_pending', 'active');

CREATE UNIQUE INDEX ella_imessage_channel_bindings_active_line_contact_key
    ON ella_imessage_channel_bindings(line_identity_hmac, contact_identity_hmac)
    WHERE status = 'active';

CREATE INDEX ella_imessage_channel_bindings_pending_proof_idx
    ON ella_imessage_channel_bindings(assigned_destination_ref_hmac, handset_ref_hmac, challenge_expires_at)
    WHERE status = 'verification_pending';

CREATE TABLE ella_imessage_proof_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    binding_id UUID NOT NULL REFERENCES ella_imessage_channel_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE,
    provider_message_ref_hmac CHAR(64) COLLATE "C" NOT NULL UNIQUE
        CHECK (provider_message_ref_hmac ~ '^[0-9a-f]{64}$'),
    outcome TEXT COLLATE "C" NOT NULL CHECK (outcome IN ('accepted', 'rejected', 'expired', 'replayed')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE OR REPLACE FUNCTION ella_reject_imessage_consent_receipt_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'ella_imessage_consent_receipts are immutable';
END;
$$;

CREATE TRIGGER ella_imessage_consent_receipts_immutable
    BEFORE UPDATE ON ella_imessage_consent_receipts
    FOR EACH ROW EXECUTE FUNCTION ella_reject_imessage_consent_receipt_mutation();

COMMIT;
