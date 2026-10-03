-- Protected server-only Standard Talk associations. No public canonical metadata
-- is a capability. This additive schema is dormant until separately reviewed
-- chat/TTS/authority integration; do not deploy it as feature activation.
BEGIN;

CREATE TABLE IF NOT EXISTS ella_standard_talk_playback_references (
    reference_sha256 TEXT COLLATE "C" PRIMARY KEY CHECK (reference_sha256 ~ '^[0-9a-f]{64}$'),
    uid TEXT COLLATE "C" NOT NULL CHECK (length(uid) BETWEEN 1 AND 512),
    event_id TEXT COLLATE "C" NOT NULL CHECK (length(event_id) BETWEEN 1 AND 512),
    source_identity TEXT COLLATE "C" NOT NULL CHECK (length(source_identity) BETWEEN 1 AND 512),
    canonical_text_sha256 TEXT COLLATE "C" NOT NULL CHECK (canonical_text_sha256 ~ '^[0-9a-f]{64}$'),
    runtime_authority_sha256 TEXT COLLATE "C" NOT NULL CHECK (runtime_authority_sha256 ~ '^[0-9a-f]{64}$'),
    consent_receipt_sha256 TEXT COLLATE "C" NOT NULL CHECK (consent_receipt_sha256 ~ '^[0-9a-f]{64}$'),
    normalization_version TEXT COLLATE "C" NOT NULL
        CHECK (normalization_version = 'standard-talk-utf16-500-emoji-v1'),
    state TEXT COLLATE "C" NOT NULL DEFAULT 'issued'
        CHECK (state IN ('issued', 'synthesizing', 'generated', 'failed', 'revoked')),
    issued_at TIMESTAMPTZ NOT NULL DEFAULT statement_timestamp(),
    expires_at TIMESTAMPTZ NOT NULL DEFAULT (statement_timestamp() + INTERVAL '5 minutes'),
    claim_id TEXT COLLATE "C" UNIQUE CHECK (claim_id ~ '^[0-9a-f]{64}$'),
    playback_id TEXT COLLATE "C" UNIQUE CHECK (playback_id ~ '^[0-9a-f]{64}$'),
    claimed_at TIMESTAMPTZ,
    terminal_at TIMESTAMPTZ,
    UNIQUE (uid, event_id, source_identity, runtime_authority_sha256, consent_receipt_sha256),
    CHECK (expires_at = issued_at + INTERVAL '5 minutes'),
    CHECK ((claim_id IS NULL) = (playback_id IS NULL)),
    CHECK ((claim_id IS NULL) = (claimed_at IS NULL)),
    CHECK (state != 'issued' OR claim_id IS NULL),
    CHECK (state NOT IN ('synthesizing', 'generated', 'failed') OR claim_id IS NOT NULL),
    CHECK ((state IN ('generated', 'failed', 'revoked')) = (terminal_at IS NOT NULL))
);

REVOKE ALL ON ella_standard_talk_playback_references FROM PUBLIC;

COMMIT;
