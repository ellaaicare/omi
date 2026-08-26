-- Causal ordering marker between managed-cloud consent and voice entitlement state.
--
-- Timestamps cannot establish ordering because CURRENT_TIMESTAMP is fixed at
-- transaction start. The shared per-user advisory lock makes the consent
-- authority revision the monotonic ordering source instead.

BEGIN;

ALTER TABLE voice_entitlements
    ADD COLUMN IF NOT EXISTS consent_authority_revision INTEGER
        CHECK (consent_authority_revision IS NULL OR consent_authority_revision >= 1);

UPDATE voice_entitlements entitlement
SET consent_authority_revision = authority.revision
FROM users account
JOIN ella_managed_cloud_consent_authority authority
  ON authority.user_id = account.id
WHERE entitlement.uid = account.omi_uid
  AND entitlement.consent_authority_revision IS NULL;

COMMENT ON COLUMN voice_entitlements.consent_authority_revision IS
    'Managed-cloud consent authority revision observed by the latest entitlement authority mutation.';

COMMIT;
