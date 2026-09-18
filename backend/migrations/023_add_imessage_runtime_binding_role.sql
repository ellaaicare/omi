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

UPDATE ella_imessage_registration_attempts
SET runtime_binding_role = CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END
WHERE runtime_binding_role IS DISTINCT FROM CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END;

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
    ADD COLUMN IF NOT EXISTS runtime_binding_role TEXT COLLATE "C" NOT NULL DEFAULT 'user';

UPDATE ella_imessage_channel_bindings
SET runtime_binding_role = CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END
WHERE runtime_binding_role IS DISTINCT FROM CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END;

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
    ADD COLUMN IF NOT EXISTS runtime_binding_role TEXT COLLATE "C" NOT NULL DEFAULT 'user';

UPDATE ella_imessage_message_receipts
SET runtime_binding_role = CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END
WHERE runtime_binding_role IS DISTINCT FROM CASE runtime_authority_kind
    WHEN 'retained_owner' THEN 'imessage'
    ELSE 'user'
END;

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
