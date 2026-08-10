-- Authority-bound generated memory and Daily Note image jobs.
--
-- Assets remain private and provider URLs are never persisted. App-facing
-- records receive only canonical-confirmed references from this ledger.

BEGIN;

CREATE TABLE IF NOT EXISTS ella_generated_image_jobs (
    job_id TEXT COLLATE "C" PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL,
    profile_binding_id TEXT COLLATE "C" NOT NULL,
    authority_generation INTEGER NOT NULL CHECK (authority_generation >= 1),
    subject_kind TEXT COLLATE "C" NOT NULL CHECK (subject_kind IN ('memory', 'daily_note')),
    subject_id TEXT COLLATE "C" NOT NULL,
    conversation_id TEXT COLLATE "C",
    memory_id TEXT COLLATE "C",
    today_card_id TEXT COLLATE "C",
    source_version_id TEXT COLLATE "C" NOT NULL,
    source_digest TEXT COLLATE "C" NOT NULL CHECK (source_digest ~ '^sha256:[0-9a-f]{64}$'),
    grounding_receipt_digest TEXT COLLATE "C" NOT NULL
        CHECK (grounding_receipt_digest ~ '^sha256:[0-9a-f]{64}$'),
    generation INTEGER NOT NULL CHECK (generation >= 1),
    prompt_contract_version TEXT COLLATE "C" NOT NULL,
    prompt_digest TEXT COLLATE "C" NOT NULL CHECK (prompt_digest ~ '^sha256:[0-9a-f]{64}$'),
    provider_id TEXT COLLATE "C" NOT NULL,
    processor_id TEXT COLLATE "C" NOT NULL,
    processor_name TEXT NOT NULL,
    model_id TEXT COLLATE "C" NOT NULL,
    consent_receipt_ref TEXT COLLATE "C" NOT NULL
        CHECK (consent_receipt_ref ~ '^sha256:[0-9a-f]{64}$'),
    consent_policy_version TEXT COLLATE "C" NOT NULL,
    consent_processor_set_hash TEXT COLLATE "C" NOT NULL
        CHECK (consent_processor_set_hash ~ '^sha256:[0-9a-f]{64}$'),
    consent_scope_version TEXT COLLATE "C" NOT NULL,
    consent_scope_hash TEXT COLLATE "C" NOT NULL CHECK (consent_scope_hash ~ '^sha256:[0-9a-f]{64}$'),
    state TEXT COLLATE "C" NOT NULL CHECK (
        state IN (
            'pending_consent', 'queued', 'generating', 'moderating',
            'awaiting_canonical', 'ready', 'rejected', 'stale', 'failed', 'cancelled'
        )
    ),
    provider_request_id TEXT COLLATE "C",
    output_asset_id UUID,
    output_asset_digest TEXT COLLATE "C"
        CHECK (output_asset_digest IS NULL OR output_asset_digest ~ '^sha256:[0-9a-f]{64}$'),
    moderation_status TEXT COLLATE "C" NOT NULL DEFAULT 'pending'
        CHECK (moderation_status IN ('pending', 'approved', 'rejected', 'failed')),
    receipt_id TEXT COLLATE "C",
    canonical_event_id TEXT COLLATE "C",
    error_code TEXT COLLATE "C",
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_generated_image_jobs_subject_shape CHECK (
        (
            subject_kind = 'memory'
            AND (conversation_id IS NOT NULL OR memory_id IS NOT NULL)
            AND today_card_id IS NULL
        )
        OR
        (subject_kind = 'daily_note' AND today_card_id IS NOT NULL AND conversation_id IS NULL AND memory_id IS NULL)
    ),
    UNIQUE (uid, subject_kind, subject_id, source_version_id, prompt_contract_version, generation)
);

CREATE INDEX IF NOT EXISTS ella_generated_image_jobs_owner_state_idx
    ON ella_generated_image_jobs (uid, state, updated_at DESC);

CREATE TABLE IF NOT EXISTS ella_generated_image_assets (
    asset_id UUID PRIMARY KEY,
    job_id TEXT COLLATE "C" NOT NULL UNIQUE REFERENCES ella_generated_image_jobs(job_id) ON DELETE CASCADE,
    uid TEXT COLLATE "C" NOT NULL,
    storage_key TEXT COLLATE "C" NOT NULL UNIQUE,
    media_type TEXT COLLATE "C" NOT NULL CHECK (media_type IN ('image/jpeg', 'image/png', 'image/webp')),
    sha256 TEXT COLLATE "C" NOT NULL CHECK (sha256 ~ '^sha256:[0-9a-f]{64}$'),
    width INTEGER NOT NULL CHECK (width BETWEEN 1 AND 16384),
    height INTEGER NOT NULL CHECK (height BETWEEN 1 AND 16384),
    moderation_status TEXT COLLATE "C" NOT NULL CHECK (moderation_status IN ('approved', 'rejected', 'failed')),
    alt_text TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ella_generated_image_receipts (
    receipt_id TEXT COLLATE "C" PRIMARY KEY,
    job_id TEXT COLLATE "C" NOT NULL UNIQUE REFERENCES ella_generated_image_jobs(job_id) ON DELETE CASCADE,
    uid TEXT COLLATE "C" NOT NULL,
    receipt JSONB NOT NULL CHECK (jsonb_typeof(receipt) = 'object'),
    canonical_status TEXT COLLATE "C" NOT NULL CHECK (canonical_status IN ('pending', 'confirmed', 'failed')),
    canonical_event_id TEXT COLLATE "C",
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ella_generated_image_receipts_canonical_shape CHECK (
        (canonical_status = 'confirmed' AND canonical_event_id IS NOT NULL)
        OR (canonical_status <> 'confirmed' AND canonical_event_id IS NULL)
    )
);

COMMIT;
