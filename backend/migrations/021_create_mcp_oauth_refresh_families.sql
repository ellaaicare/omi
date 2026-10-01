-- Dormant OAuth refresh credentials. No grants, clients, or runtime attachments
-- are created by this migration. Token digests are SHA-256 of random secrets.
BEGIN;

CREATE TABLE ella_mcp_refresh_families (
    id UUID PRIMARY KEY,
    account_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    profile_user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    client_id TEXT NOT NULL,
    authority JSONB NOT NULL,
    authority_digest CHAR(64) COLLATE "C" NOT NULL CHECK (authority_digest ~ '^[0-9a-f]{64}$'),
    expires_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE ella_mcp_refresh_tokens (
    digest CHAR(64) COLLATE "C" PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{64}$'),
    family_id UUID NOT NULL REFERENCES ella_mcp_refresh_families(id) ON DELETE CASCADE,
    consumed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX ella_mcp_refresh_tokens_family_idx ON ella_mcp_refresh_tokens(family_id);
COMMIT;
