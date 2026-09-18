-- Bind every iMessage authority snapshot to its exact runtime-binding role.
--
-- Migration 022 was published before runtime_binding_role was introduced.
-- This additive successor supports both databases that already applied that
-- published migration and fresh installs applying the complete chain.  The
-- IF NOT EXISTS clauses also make it safe for an environment that received
-- the expanded 022 shape before this successor was introduced.

BEGIN;

ALTER TABLE ella_imessage_registration_attempts
    ADD COLUMN IF NOT EXISTS runtime_binding_role TEXT COLLATE "C" NOT NULL DEFAULT 'user';

ALTER TABLE ella_imessage_channel_bindings
    ADD COLUMN IF NOT EXISTS runtime_binding_role TEXT COLLATE "C" NOT NULL DEFAULT 'user';

ALTER TABLE ella_imessage_message_receipts
    ADD COLUMN IF NOT EXISTS runtime_binding_role TEXT COLLATE "C" NOT NULL DEFAULT 'user';

-- Published migration 022 allowed a retained-owner snapshot to reference the
-- ordinary role=user runtime.  Its digest is bound to that exact authority, so
-- migration SQL cannot safely fabricate or repoint a dedicated role=imessage
-- runtime.  Adding the columns above gives published rows the role=user
-- default; this preflight then aborts the transaction (including those column
-- additions) unless every existing snapshot already agrees with the referenced
-- runtime binding.  A legacy retained graph therefore requires the explicit,
-- provider-confirmed retirement/re-enrollment path.
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM ella_imessage_registration_attempts snapshot
        JOIN ella_runtime_bindings runtime ON runtime.id = snapshot.runtime_binding_id
        WHERE runtime.user_id IS DISTINCT FROM snapshot.user_id
           OR (
                snapshot.runtime_authority_kind = 'target'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'user'
                    OR snapshot.runtime_target_id IS NULL
                    OR runtime.role IS DISTINCT FROM 'user'
                )
           )
           OR (
                snapshot.runtime_authority_kind = 'retained_owner'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'imessage'
                    OR snapshot.runtime_target_id IS NOT NULL
                    OR runtime.role IS DISTINCT FROM 'imessage'
                )
           )
           OR snapshot.runtime_authority_kind NOT IN ('target', 'retained_owner')

        UNION ALL

        SELECT 1
        FROM ella_imessage_channel_bindings snapshot
        JOIN ella_runtime_bindings runtime ON runtime.id = snapshot.runtime_binding_id
        WHERE runtime.user_id IS DISTINCT FROM snapshot.user_id
           OR (
                snapshot.runtime_authority_kind = 'target'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'user'
                    OR snapshot.runtime_target_id IS NULL
                    OR runtime.role IS DISTINCT FROM 'user'
                )
           )
           OR (
                snapshot.runtime_authority_kind = 'retained_owner'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'imessage'
                    OR snapshot.runtime_target_id IS NOT NULL
                    OR runtime.role IS DISTINCT FROM 'imessage'
                )
           )
           OR snapshot.runtime_authority_kind NOT IN ('target', 'retained_owner')

        UNION ALL

        SELECT 1
        FROM ella_imessage_message_receipts snapshot
        JOIN ella_runtime_bindings runtime ON runtime.id = snapshot.runtime_binding_id
        WHERE runtime.user_id IS DISTINCT FROM snapshot.user_id
           OR (
                snapshot.runtime_authority_kind = 'target'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'user'
                    OR snapshot.runtime_target_id IS NULL
                    OR runtime.role IS DISTINCT FROM 'user'
                )
           )
           OR (
                snapshot.runtime_authority_kind = 'retained_owner'
                AND (
                    snapshot.runtime_binding_role IS DISTINCT FROM 'imessage'
                    OR snapshot.runtime_target_id IS NOT NULL
                    OR runtime.role IS DISTINCT FROM 'imessage'
                )
           )
           OR snapshot.runtime_authority_kind NOT IN ('target', 'retained_owner')
    ) THEN
        RAISE EXCEPTION USING
            ERRCODE = '23514',
            MESSAGE = 'imessage_legacy_runtime_authority_requires_retirement';
    END IF;
END
$$;

ALTER TABLE ella_imessage_registration_attempts
    DROP CONSTRAINT IF EXISTS ella_imessage_registration_attempts_runtime_authority_shape,
    ADD CONSTRAINT ella_imessage_registration_attempts_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_binding_role = 'user' AND runtime_target_id IS NOT NULL)
        OR (
            runtime_authority_kind = 'retained_owner'
            AND runtime_binding_role = 'imessage'
            AND runtime_target_id IS NULL
        )
    );

ALTER TABLE ella_imessage_channel_bindings
    DROP CONSTRAINT IF EXISTS ella_imessage_channel_bindings_runtime_authority_shape,
    ADD CONSTRAINT ella_imessage_channel_bindings_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_binding_role = 'user' AND runtime_target_id IS NOT NULL)
        OR (
            runtime_authority_kind = 'retained_owner'
            AND runtime_binding_role = 'imessage'
            AND runtime_target_id IS NULL
        )
    );

ALTER TABLE ella_imessage_message_receipts
    DROP CONSTRAINT IF EXISTS ella_imessage_message_receipts_runtime_authority_shape,
    ADD CONSTRAINT ella_imessage_message_receipts_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_binding_role = 'user' AND runtime_target_id IS NOT NULL)
        OR (
            runtime_authority_kind = 'retained_owner'
            AND runtime_binding_role = 'imessage'
            AND runtime_target_id IS NULL
        )
    );

COMMIT;
