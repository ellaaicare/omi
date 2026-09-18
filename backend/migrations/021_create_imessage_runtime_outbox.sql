-- Durable self-hosted iMessage runtime receipt and delivery outbox.
--
-- This is deliberately separate from migration 009's one-owner Hermes Cloud
-- Photon canary. The transport supplies opaque message and connection
-- identities only; owner and runtime authority are derived from the active
-- enrollment binding created by migration 020.

BEGIN;

ALTER TABLE ella_imessage_channel_bindings
    ADD COLUMN transport_connection_ref_hmac CHAR(64) COLLATE "C"
        CHECK (
            transport_connection_ref_hmac IS NULL
            OR transport_connection_ref_hmac ~ '^[0-9a-f]{64}$'
        ),
    ADD COLUMN transport_connected_at TIMESTAMPTZ;

CREATE TABLE ella_imessage_message_receipts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    binding_id UUID NOT NULL
        REFERENCES ella_imessage_channel_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    inbound_provider_ref_hmac CHAR(64) COLLATE "C" NOT NULL
        CHECK (inbound_provider_ref_hmac ~ '^[0-9a-f]{64}$'),
    inbound_payload_sha256 CHAR(64) COLLATE "C" NOT NULL
        CHECK (inbound_payload_sha256 ~ '^[0-9a-f]{64}$'),
    message_text TEXT NOT NULL CHECK (octet_length(message_text) BETWEEN 1 AND 32768),
    occurred_at TIMESTAMPTZ NOT NULL,
    binding_generation INTEGER NOT NULL CHECK (binding_generation >= 1),
    consent_receipt_id UUID NOT NULL,
    consent_authority_epoch UUID NOT NULL,
    runtime_binding_id UUID NOT NULL
        REFERENCES ella_runtime_bindings(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_target_id UUID NOT NULL
        REFERENCES ella_runtime_targets(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    runtime_authority_digest CHAR(64) COLLATE "C" NOT NULL
        CHECK (runtime_authority_digest ~ '^[0-9a-f]{64}$'),
    status TEXT COLLATE "C" NOT NULL DEFAULT 'claimed'
        CHECK (status IN (
            'claimed', 'running', 'awaiting_delivery', 'sending', 'delivered',
            'failed', 'uncertain', 'quarantined'
        )),
    delivery_idempotency_key UUID NOT NULL DEFAULT gen_random_uuid(),
    canonical_inbound_event_id TEXT,
    canonical_outbound_event_id TEXT,
    outbound_text TEXT CHECK (
        outbound_text IS NULL OR octet_length(outbound_text) BETWEEN 1 AND 32768
    ),
    runtime_revision INTEGER CHECK (runtime_revision IS NULL OR runtime_revision >= 1),
    runtime_agent_id TEXT,
    model_started BOOLEAN NOT NULL DEFAULT false,
    send_started BOOLEAN NOT NULL DEFAULT false,
    outbound_provider_ref_hmac CHAR(64) COLLATE "C"
        CHECK (
            outbound_provider_ref_hmac IS NULL
            OR outbound_provider_ref_hmac ~ '^[0-9a-f]{64}$'
        ),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count >= 1),
    error_code TEXT,
    reconciliation_status TEXT COLLATE "C" NOT NULL DEFAULT 'none'
        CHECK (reconciliation_status IN ('none', 'recovered', 'manual_required')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ella_imessage_message_receipts_consent_fkey
        FOREIGN KEY (user_id, consent_receipt_id)
        REFERENCES ella_imessage_consent_receipts(user_id, id)
        ON DELETE RESTRICT ON UPDATE CASCADE,
    CONSTRAINT ella_imessage_message_receipts_outbound_shape CHECK (
        (
            status IN ('awaiting_delivery', 'sending', 'delivered')
            AND canonical_inbound_event_id IS NOT NULL
            AND canonical_outbound_event_id IS NOT NULL
            AND outbound_text IS NOT NULL
        )
        OR status NOT IN ('awaiting_delivery', 'sending', 'delivered')
    ),
    CONSTRAINT ella_imessage_message_receipts_send_shape CHECK (
        (status = 'sending' AND send_started)
        OR status <> 'sending'
    ),
    UNIQUE (binding_id, inbound_provider_ref_hmac),
    UNIQUE (delivery_idempotency_key)
);

CREATE UNIQUE INDEX ella_imessage_message_receipts_outbound_provider_key
    ON ella_imessage_message_receipts(binding_id, outbound_provider_ref_hmac)
    WHERE outbound_provider_ref_hmac IS NOT NULL;

CREATE INDEX ella_imessage_message_receipts_outbox_idx
    ON ella_imessage_message_receipts(binding_id, binding_generation, status, updated_at);

COMMIT;
