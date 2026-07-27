-- Hermes Cloud warm-pool claims, opaque session identity, and canonical
-- ingestion ownership. Apply only after the control-plane schema PR is merged.

ALTER TABLE ella_runtime_bindings
    DROP CONSTRAINT ella_runtime_bindings_user_id_fkey;

ALTER TABLE ella_runtime_bindings
    ALTER COLUMN user_id DROP NOT NULL,
    ADD COLUMN status TEXT NOT NULL DEFAULT 'disabled',
    ADD COLUMN runtime_instance_id TEXT,
    ADD COLUMN api_base_url_ref TEXT,
    ADD COLUMN api_key_ref TEXT,
    ADD COLUMN honcho_api_key_ref TEXT,
    ADD COLUMN prompt_pack_version TEXT,
    ADD COLUMN prompt_artifact_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN expected_model TEXT,
    ADD COLUMN allowed_tools JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN required_capabilities JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN claim_job_id UUID,
    ADD COLUMN claim_token UUID,
    ADD COLUMN claim_lease_expires_at TIMESTAMPTZ,
    ADD COLUMN claimed_at TIMESTAMPTZ,
    ADD COLUMN disabled_at TIMESTAMPTZ,
    ADD COLUMN quarantined_at TIMESTAMPTZ,
    ADD COLUMN quarantine_reason TEXT;

ALTER TABLE ella_provisioning_jobs
    ADD COLUMN external_side_effects JSONB NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN rollback_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN manual_intervention_at TIMESTAMPTZ;

UPDATE ella_runtime_bindings
SET status = CASE WHEN active THEN 'active' ELSE 'disabled' END;

DROP INDEX ella_runtime_bindings_user_role_provider_key;
CREATE UNIQUE INDEX ella_runtime_bindings_user_role_provider_key
    ON ella_runtime_bindings(user_id, role, provider)
    WHERE provider <> 'hermes_cloud';

ALTER TABLE ella_runtime_bindings
    ADD CONSTRAINT ella_runtime_bindings_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE,
    ADD CONSTRAINT ella_runtime_bindings_claim_job_id_fkey
    FOREIGN KEY (claim_job_id) REFERENCES ella_provisioning_jobs(id) ON DELETE SET NULL ON UPDATE CASCADE,
    ADD CONSTRAINT ella_runtime_bindings_status_check
    CHECK (status IN (
        'pool_available', 'claiming', 'shadow', 'internal_canary',
        'active', 'quarantined', 'disabled'
    )),
    ADD CONSTRAINT ella_runtime_bindings_cloud_pool_shape_check
    CHECK (
        provider <> 'hermes_cloud'
        OR (
            (
                status = 'pool_available'
                AND user_id IS NULL
                AND claim_job_id IS NULL
                AND claim_token IS NULL
                AND honcho_workspace IS NULL
                AND observed_peer IS NULL
                AND observer_peer IS NULL
                AND active = false
            )
            OR (
                status = 'claiming'
                AND user_id IS NOT NULL
                AND claim_job_id IS NOT NULL
                AND claim_token IS NOT NULL
                AND claim_lease_expires_at IS NOT NULL
                AND active = false
            )
            OR (
                status IN ('shadow', 'internal_canary', 'active')
                AND user_id IS NOT NULL
                AND runtime_instance_id IS NOT NULL
                AND api_base_url_ref IS NOT NULL
                AND api_key_ref IS NOT NULL
                AND prompt_artifact_receipt <> '{}'::jsonb
                AND honcho_workspace IS NOT NULL
                AND observed_peer IS NOT NULL
                AND observer_peer IS NOT NULL
                AND active = (status <> 'shadow')
            )
            OR status IN ('quarantined', 'disabled')
        )
    );

CREATE UNIQUE INDEX ella_runtime_bindings_runtime_instance_key
    ON ella_runtime_bindings(runtime_instance_id)
    WHERE runtime_instance_id IS NOT NULL;
CREATE UNIQUE INDEX ella_runtime_bindings_claim_job_key
    ON ella_runtime_bindings(claim_job_id)
    WHERE claim_job_id IS NOT NULL
      AND status IN ('claiming', 'shadow', 'internal_canary', 'active');
CREATE UNIQUE INDEX ella_runtime_bindings_claim_token_key
    ON ella_runtime_bindings(claim_token)
    WHERE claim_token IS NOT NULL;
CREATE INDEX ella_runtime_bindings_pool_lookup_idx
    ON ella_runtime_bindings(provider, status, health_state, updated_at);

CREATE TABLE ella_runtime_session_scopes (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    binding_id UUID NOT NULL,
    user_id UUID NOT NULL,
    role TEXT NOT NULL DEFAULT 'user',
    channel TEXT NOT NULL,
    session_key TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_runtime_session_scopes_pkey PRIMARY KEY (id),
    CONSTRAINT ella_runtime_session_scopes_binding_id_fkey
        FOREIGN KEY (binding_id) REFERENCES ella_runtime_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT ella_runtime_session_scopes_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX ella_runtime_session_scopes_session_key
    ON ella_runtime_session_scopes(session_key);
CREATE UNIQUE INDEX ella_runtime_session_scopes_binding_role_channel_key
    ON ella_runtime_session_scopes(binding_id, role, channel);
CREATE INDEX ella_runtime_session_scopes_user_updated_idx
    ON ella_runtime_session_scopes(user_id, updated_at);

CREATE TABLE ella_runtime_interactions (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    scope_id UUID NOT NULL,
    client_interaction_id TEXT NOT NULL,
    request_hash VARCHAR(64) NOT NULL,
    hermes_session_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    previous_response_id TEXT,
    provider_response_id TEXT,
    canonical_user_event_id TEXT,
    canonical_assistant_event_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'running', 'completed', 'failed', 'cancelled')),
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    error_code TEXT,
    started_at TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_runtime_interactions_pkey PRIMARY KEY (id),
    CONSTRAINT ella_runtime_interactions_scope_id_fkey
        FOREIGN KEY (scope_id) REFERENCES ella_runtime_session_scopes(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX ella_runtime_interactions_scope_client_key
    ON ella_runtime_interactions(scope_id, client_interaction_id);
CREATE UNIQUE INDEX ella_runtime_interactions_hermes_session_key
    ON ella_runtime_interactions(hermes_session_id);
CREATE UNIQUE INDEX ella_runtime_interactions_idempotency_key
    ON ella_runtime_interactions(idempotency_key);
CREATE INDEX ella_runtime_interactions_scope_status_idx
    ON ella_runtime_interactions(scope_id, status, updated_at);

CREATE TABLE ella_runtime_ingestion_receipts (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    binding_id UUID NOT NULL,
    canonical_event_id TEXT NOT NULL,
    source_identity TEXT NOT NULL,
    event_revision INTEGER NOT NULL DEFAULT 1 CHECK (event_revision >= 1),
    provenance TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN ('claimed', 'written', 'skipped', 'failed')),
    provider_ref TEXT,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_runtime_ingestion_receipts_pkey PRIMARY KEY (id),
    CONSTRAINT ella_runtime_ingestion_receipts_binding_id_fkey
        FOREIGN KEY (binding_id) REFERENCES ella_runtime_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX ella_runtime_ingestion_event_revision_key
    ON ella_runtime_ingestion_receipts(
        binding_id, canonical_event_id, source_identity, event_revision
    );
CREATE INDEX ella_runtime_ingestion_status_idx
    ON ella_runtime_ingestion_receipts(binding_id, status, updated_at);

CREATE TABLE ella_runtime_pool_alerts (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    provider TEXT NOT NULL,
    alert_type TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'delivered', 'resolved')),
    available_count INTEGER NOT NULL CHECK (available_count >= 0),
    threshold INTEGER NOT NULL CHECK (threshold > 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    delivered_at TIMESTAMPTZ,
    resolved_at TIMESTAMPTZ,
    CONSTRAINT ella_runtime_pool_alerts_pkey PRIMARY KEY (id)
);

CREATE UNIQUE INDEX ella_runtime_pool_alerts_one_pending_key
    ON ella_runtime_pool_alerts(provider, alert_type)
    WHERE state = 'pending';
CREATE INDEX ella_runtime_pool_alerts_provider_state_idx
    ON ella_runtime_pool_alerts(provider, state, created_at);

ALTER TABLE voice_kill_switches
    DROP CONSTRAINT voice_kill_switches_scope_type_check,
    ADD CONSTRAINT voice_kill_switches_scope_type_check
    CHECK (scope_type IN ('global', 'user', 'provider', 'channel'));

CREATE TABLE ella_photon_channel_bindings (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    runtime_binding_id UUID NOT NULL,
    user_id UUID NOT NULL,
    role TEXT NOT NULL DEFAULT 'internal-owner'
        CHECK (role = 'internal-owner'),
    status TEXT NOT NULL DEFAULT 'disabled'
        CHECK (status IN ('disabled', 'enabled', 'quarantined')),
    line_identity_key VARCHAR(64) NOT NULL CHECK (length(line_identity_key) = 64),
    contact_identity_key VARCHAR(64) NOT NULL CHECK (length(contact_identity_key) = 64),
    policy_commit_sha VARCHAR(40) NOT NULL CHECK (length(policy_commit_sha) = 40),
    command_tier_version TEXT NOT NULL,
    allow_all BOOLEAN NOT NULL DEFAULT false CHECK (allow_all = false),
    attachments_enabled BOOLEAN NOT NULL DEFAULT false CHECK (attachments_enabled = false),
    caregiver_delivery_enabled BOOLEAN NOT NULL DEFAULT false
        CHECK (caregiver_delivery_enabled = false),
    rollout_phase INTEGER NOT NULL DEFAULT 3 CHECK (rollout_phase = 3),
    daily_message_limit INTEGER NOT NULL CHECK (
        daily_message_limit >= 2 AND daily_message_limit < 5000
    ),
    daily_initiation_limit INTEGER NOT NULL CHECK (
        daily_initiation_limit > 0 AND daily_initiation_limit < 50
    ),
    sidecar_connection_key VARCHAR(64),
    sidecar_connected_at TIMESTAMPTZ,
    oauth_expires_at TIMESTAMPTZ,
    preflight_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    quarantined_at TIMESTAMPTZ,
    quarantine_reason TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_photon_channel_bindings_pkey PRIMARY KEY (id),
    CONSTRAINT ella_photon_channel_bindings_runtime_binding_id_fkey
        FOREIGN KEY (runtime_binding_id)
        REFERENCES ella_runtime_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT ella_photon_channel_bindings_user_id_fkey
        FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE ON UPDATE CASCADE
);

CREATE UNIQUE INDEX ella_photon_channel_bindings_runtime_key
    ON ella_photon_channel_bindings(runtime_binding_id);
CREATE UNIQUE INDEX ella_photon_channel_bindings_identity_key
    ON ella_photon_channel_bindings(line_identity_key, contact_identity_key);
CREATE UNIQUE INDEX ella_photon_channel_bindings_one_owner_key
    ON ella_photon_channel_bindings(role)
    WHERE status = 'enabled';
CREATE INDEX ella_photon_channel_bindings_status_idx
    ON ella_photon_channel_bindings(status, updated_at);

CREATE TABLE ella_photon_message_receipts (
    id UUID NOT NULL DEFAULT gen_random_uuid(),
    photon_binding_id UUID NOT NULL,
    inbound_provider_message_key VARCHAR(64) NOT NULL
        CHECK (length(inbound_provider_message_key) = 64),
    inbound_payload_sha256 VARCHAR(64) NOT NULL
        CHECK (length(inbound_payload_sha256) = 64),
    outbound_provider_message_key VARCHAR(64),
    delivery_idempotency_key UUID NOT NULL DEFAULT gen_random_uuid(),
    status TEXT NOT NULL DEFAULT 'claimed'
        CHECK (status IN (
            'claimed', 'running', 'awaiting_delivery', 'delivered',
            'failed', 'uncertain'
        )),
    runtime_interaction_id UUID,
    canonical_inbound_event_id TEXT,
    canonical_outbound_event_id TEXT,
    runtime_revision INTEGER,
    expected_model TEXT,
    policy_commit_sha VARCHAR(40),
    command_tier_version TEXT,
    usage JSONB NOT NULL DEFAULT '{}'::jsonb,
    preflight_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    writeback_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    delivery_receipt JSONB NOT NULL DEFAULT '{}'::jsonb,
    quota_reserved BOOLEAN NOT NULL DEFAULT false,
    error_code TEXT,
    provider_started BOOLEAN NOT NULL DEFAULT false,
    attempt_count INTEGER NOT NULL DEFAULT 1 CHECK (attempt_count > 0),
    lease_token UUID,
    lease_expires_at TIMESTAMPTZ,
    reconciliation_status TEXT NOT NULL DEFAULT 'none'
        CHECK (reconciliation_status IN (
            'none', 'recovered', 'manual_required'
        )),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMPTZ,
    CONSTRAINT ella_photon_message_receipts_pkey PRIMARY KEY (id),
    CONSTRAINT ella_photon_message_receipts_binding_id_fkey
        FOREIGN KEY (photon_binding_id)
        REFERENCES ella_photon_channel_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE,
    CONSTRAINT ella_photon_message_receipts_runtime_interaction_id_fkey
        FOREIGN KEY (runtime_interaction_id)
        REFERENCES ella_runtime_interactions(id) ON DELETE SET NULL ON UPDATE CASCADE
);

CREATE UNIQUE INDEX ella_photon_message_receipts_inbound_key
    ON ella_photon_message_receipts(
        photon_binding_id, inbound_provider_message_key
    );
CREATE UNIQUE INDEX ella_photon_message_receipts_outbound_key
    ON ella_photon_message_receipts(
        photon_binding_id, outbound_provider_message_key
    )
    WHERE outbound_provider_message_key IS NOT NULL;
CREATE UNIQUE INDEX ella_photon_message_receipts_delivery_key
    ON ella_photon_message_receipts(delivery_idempotency_key);
CREATE INDEX ella_photon_message_receipts_status_idx
    ON ella_photon_message_receipts(photon_binding_id, status, updated_at);

CREATE TABLE ella_photon_quota_buckets (
    photon_binding_id UUID NOT NULL,
    bucket_date DATE NOT NULL,
    messages_reserved INTEGER NOT NULL DEFAULT 0 CHECK (messages_reserved >= 0),
    initiations_reserved INTEGER NOT NULL DEFAULT 0 CHECK (initiations_reserved >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_photon_quota_buckets_pkey
        PRIMARY KEY (photon_binding_id, bucket_date),
    CONSTRAINT ella_photon_quota_buckets_binding_id_fkey
        FOREIGN KEY (photon_binding_id)
        REFERENCES ella_photon_channel_bindings(id) ON DELETE CASCADE ON UPDATE CASCADE
);
