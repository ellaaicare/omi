-- Content-free lifecycle receipts for the root-only synthetic invitation
-- operator. No invitation format, target binding, or user schema changes.

BEGIN;

ALTER TABLE ella_invitation_audit_receipts
    DROP CONSTRAINT IF EXISTS ella_invitation_audit_receipts_event_type_check;

ALTER TABLE ella_invitation_audit_receipts
    ADD CONSTRAINT ella_invitation_audit_receipts_event_type_check
    CHECK (event_type IN (
        'redeemed', 'idempotent_retry', 'invalid', 'expired', 'capacity',
        'rate_limited', 'policy_invalid', 'redemption_disabled',
        'operator_issued', 'operator_idempotent_retry', 'operator_revoked',
        'operator_cleanup'
    ));

COMMIT;
