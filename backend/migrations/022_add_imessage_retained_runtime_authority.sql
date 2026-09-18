-- Explicit retained-owner authority for the self-hosted iMessage lane.
--
-- Invitation-owned bindings continue to require an exact runtime target.  The
-- one configured retained owner predates runtime targets, so its iMessage
-- graph records a distinct authority kind and a NULL target while remaining
-- pinned to the exact active runtime binding and authority digest.

BEGIN;

ALTER TABLE ella_imessage_registration_attempts
    ADD COLUMN runtime_authority_kind TEXT COLLATE "C" NOT NULL DEFAULT 'target',
    ALTER COLUMN runtime_target_id DROP NOT NULL;

ALTER TABLE ella_imessage_registration_attempts
    ADD CONSTRAINT ella_imessage_registration_attempts_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_target_id IS NOT NULL)
        OR (runtime_authority_kind = 'retained_owner' AND runtime_target_id IS NULL)
    );

ALTER TABLE ella_imessage_channel_bindings
    ADD COLUMN runtime_authority_kind TEXT COLLATE "C" NOT NULL DEFAULT 'target',
    ALTER COLUMN runtime_target_id DROP NOT NULL;

ALTER TABLE ella_imessage_channel_bindings
    ADD CONSTRAINT ella_imessage_channel_bindings_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_target_id IS NOT NULL)
        OR (runtime_authority_kind = 'retained_owner' AND runtime_target_id IS NULL)
    );

ALTER TABLE ella_imessage_message_receipts
    ADD COLUMN runtime_authority_kind TEXT COLLATE "C" NOT NULL DEFAULT 'target',
    ALTER COLUMN runtime_target_id DROP NOT NULL;

ALTER TABLE ella_imessage_message_receipts
    ADD CONSTRAINT ella_imessage_message_receipts_runtime_authority_shape CHECK (
        (runtime_authority_kind = 'target' AND runtime_target_id IS NOT NULL)
        OR (runtime_authority_kind = 'retained_owner' AND runtime_target_id IS NULL)
    );

COMMIT;
