-- Add optional email-scoping to invitation codes.
--
-- When allowed_email_hash is set, redemption checks that the authenticated
-- user's normalized email (lowercased, trimmed) hashes to the stored value.
-- When NULL, any authenticated user may redeem the code.
--
-- Hash is SHA-256 of parameterized pepper + lowercased email, so the plaintext
-- email is never stored.

ALTER TABLE ella_invitations
  ADD COLUMN IF NOT EXISTS allowed_email_hash TEXT COLLATE "C"
    CHECK (allowed_email_hash IS NULL OR allowed_email_hash ~ '^[0-9a-f]{64}$');

COMMENT ON COLUMN ella_invitations.allowed_email_hash IS
  'SHA-256 HMAC of (pepper + normalized email). NULL = open to any authenticated user.';
