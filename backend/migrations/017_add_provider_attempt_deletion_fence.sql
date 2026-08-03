-- Durable, content-free proof that an external provisioning call may have
-- created provider state. Apply after 015; migration 016 is reserved by
-- ellaaicare/omi#360 and is intentionally not a prerequisite for this change.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS ella_provider_attempts (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id UUID NOT NULL
        REFERENCES users(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    provisioning_job_id UUID NOT NULL
        REFERENCES ella_provisioning_jobs(id) ON DELETE RESTRICT ON UPDATE CASCADE,
    provider TEXT NOT NULL CHECK (provider IN ('hermes')),
    operation TEXT NOT NULL CHECK (operation IN ('provision')),
    idempotency_key UUID NOT NULL,
    correlation_ref TEXT COLLATE "C" NOT NULL
        CHECK (correlation_ref ~ '^ella-ext-[0-9a-f]{16}$'),
    proof_state TEXT NOT NULL DEFAULT 'unproven'
        CHECK (proof_state IN ('unproven', 'deprovisioned', 'absence_proven')),
    content_free BOOLEAN NOT NULL DEFAULT TRUE CHECK (content_free = TRUE),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    proved_at TIMESTAMPTZ,
    CONSTRAINT ella_provider_attempts_proof_shape_check CHECK (
        (proof_state = 'unproven' AND proved_at IS NULL)
        OR (proof_state IN ('deprovisioned', 'absence_proven') AND proved_at IS NOT NULL)
    )
);

CREATE UNIQUE INDEX IF NOT EXISTS ella_provider_attempts_idempotency_key
    ON ella_provider_attempts(idempotency_key);
CREATE UNIQUE INDEX IF NOT EXISTS ella_provider_attempts_job_operation_key
    ON ella_provider_attempts(provisioning_job_id, provider, operation);
CREATE UNIQUE INDEX IF NOT EXISTS ella_provider_attempts_correlation_ref_key
    ON ella_provider_attempts(correlation_ref);
CREATE INDEX IF NOT EXISTS ella_provider_attempts_user_pending_idx
    ON ella_provider_attempts(user_id, created_at)
    WHERE proof_state = 'unproven';

COMMENT ON TABLE ella_provider_attempts IS
    'Content-free attempt markers written before provider calls; unproven rows block account-deletion completion.';
COMMENT ON COLUMN ella_provider_attempts.idempotency_key IS
    'Stable provider request key reused for every retry of one provisioning job operation.';
COMMENT ON COLUMN ella_provider_attempts.correlation_ref IS
    'Non-secret operator reference safe for typed deletion-pending receipts.';

-- Defense in depth for every current/future authority writer. PENDING remains
-- a narrowly supported bootstrap state; deletion tombstones are irreversible
-- except for the idempotent DELETION_PENDING -> DELETED finalization.
CREATE OR REPLACE FUNCTION ella_require_user_not_tombstoned_uuid()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner_status TEXT;
BEGIN
    IF NEW.user_id IS NULL THEN
        RETURN NEW;
    END IF;
    SELECT status INTO owner_status FROM users WHERE id = NEW.user_id;
    IF owner_status IS NULL OR owner_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_require_user_not_tombstoned_uid()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner_status TEXT;
BEGIN
    SELECT status INTO owner_status FROM users WHERE omi_uid = NEW.uid;
    IF owner_status IS NULL OR owner_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_require_target_owner_not_tombstoned()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    account_status TEXT;
    profile_status TEXT;
BEGIN
    SELECT status INTO account_status FROM users WHERE id = NEW.account_user_id;
    SELECT status INTO profile_status FROM users WHERE id = NEW.profile_user_id;
    IF account_status IS NULL OR profile_status IS NULL
       OR account_status IN ('DELETION_PENDING', 'DELETED')
       OR profile_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_require_scope_owner_not_tombstoned()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner_status TEXT;
BEGIN
    SELECT u.status INTO owner_status
    FROM ella_runtime_session_scopes scope
    JOIN users u ON u.id = scope.user_id
    WHERE scope.id = NEW.scope_id;
    IF owner_status IS NULL OR owner_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_require_binding_owner_not_tombstoned()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner_status TEXT;
BEGIN
    SELECT u.status INTO owner_status
    FROM ella_runtime_bindings binding
    JOIN users u ON u.id = binding.user_id
    WHERE binding.id = NEW.binding_id;
    IF owner_status IS NULL OR owner_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_require_photon_owner_not_tombstoned()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE
    owner_status TEXT;
BEGIN
    SELECT u.status INTO owner_status
    FROM ella_photon_channel_bindings photon
    JOIN users u ON u.id = photon.user_id
    WHERE photon.id = NEW.photon_binding_id;
    IF owner_status IS NULL OR owner_status IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

CREATE OR REPLACE FUNCTION ella_fence_user_tombstone_transition()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.status = 'DELETED' AND NEW.status <> 'DELETED' THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    IF OLD.status = 'DELETION_PENDING'
       AND NEW.status NOT IN ('DELETION_PENDING', 'DELETED') THEN
        RAISE EXCEPTION 'authority_write_user_not_active' USING ERRCODE = '55000';
    END IF;
    RETURN NEW;
END
$$;

DROP TRIGGER IF EXISTS ella_users_tombstone_write_fence ON users;
CREATE TRIGGER ella_users_tombstone_write_fence
BEFORE UPDATE ON users
FOR EACH ROW EXECUTE FUNCTION ella_fence_user_tombstone_transition();

DO $$
DECLARE
    table_name TEXT;
BEGIN
    FOREACH table_name IN ARRAY ARRAY[
        'ella_managed_cloud_consent_authority',
        'ella_invitation_redemptions',
        'ella_provisioning_jobs',
        'ella_runtime_bindings',
        'ella_runtime_session_scopes',
        'ella_photon_channel_bindings',
        'agent_clusters'
    ] LOOP
        IF to_regclass(current_schema() || '.' || table_name) IS NOT NULL THEN
            EXECUTE format('DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON %I', table_name);
            EXECUTE format(
                'CREATE TRIGGER ella_tombstone_write_fence BEFORE INSERT OR UPDATE ON %I '
                'FOR EACH ROW EXECUTE FUNCTION ella_require_user_not_tombstoned_uuid()',
                table_name
            );
        END IF;
    END LOOP;
END
$$;

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON voice_entitlements;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON voice_entitlements
FOR EACH ROW EXECUTE FUNCTION ella_require_user_not_tombstoned_uid();

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON ella_runtime_targets;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON ella_runtime_targets
FOR EACH ROW EXECUTE FUNCTION ella_require_target_owner_not_tombstoned();

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON ella_runtime_interactions;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON ella_runtime_interactions
FOR EACH ROW EXECUTE FUNCTION ella_require_scope_owner_not_tombstoned();

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON ella_runtime_ingestion_receipts;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON ella_runtime_ingestion_receipts
FOR EACH ROW EXECUTE FUNCTION ella_require_binding_owner_not_tombstoned();

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON ella_photon_message_receipts;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON ella_photon_message_receipts
FOR EACH ROW EXECUTE FUNCTION ella_require_photon_owner_not_tombstoned();

DROP TRIGGER IF EXISTS ella_tombstone_write_fence ON ella_photon_quota_buckets;
CREATE TRIGGER ella_tombstone_write_fence
BEFORE INSERT OR UPDATE ON ella_photon_quota_buckets
FOR EACH ROW EXECUTE FUNCTION ella_require_photon_owner_not_tombstoned();

COMMIT;
