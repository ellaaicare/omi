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
