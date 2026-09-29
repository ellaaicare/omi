-- Distinguish consent-owned entitlement quarantine from operator authority.
--
-- Existing revoked rows fail closed. Only a managed-consent transition may
-- mark an entitlement recoverable, and normal activation consumes the marker.

BEGIN;

ALTER TABLE voice_entitlements
    ADD COLUMN IF NOT EXISTS managed_consent_recoverable BOOLEAN NOT NULL DEFAULT FALSE;

COMMENT ON COLUMN voice_entitlements.managed_consent_recoverable IS
    'True only while managed-consent quarantine may be recovered by a later valid grant.';

COMMIT;
