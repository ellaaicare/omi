-- Canonical app-facing daily companion artifacts.
--
-- Firestore daily_summaries remain a legacy input surface only. This table is
-- the versioned app and voice-scope authority for ella.today_card.v1.

BEGIN;

CREATE TABLE IF NOT EXISTS ella_today_cards (
    card_id UUID PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL,
    local_date DATE NOT NULL,
    timezone TEXT COLLATE "C" NOT NULL,
    contract_version TEXT COLLATE "C" NOT NULL,
    version INTEGER NOT NULL DEFAULT 1 CHECK (version >= 1),
    state TEXT COLLATE "C" NOT NULL
        CHECK (state IN ('ready', 'preparing', 'new_user', 'degraded')),
    kind TEXT COLLATE "C"
        CHECK (kind IS NULL OR kind IN ('recap', 'memory', 'interest', 'welcome')),
    content JSONB,
    source_refs JSONB NOT NULL DEFAULT '[]'::jsonb
        CHECK (jsonb_typeof(source_refs) = 'array'),
    evidence_hash TEXT COLLATE "C"
        CHECK (evidence_hash IS NULL OR evidence_hash ~ '^sha256:[0-9a-f]{64}$'),
    source_watermark TEXT COLLATE "C",
    render_contract_version TEXT COLLATE "C" NOT NULL,
    private_consolidation JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(private_consolidation) = 'object'),
    presentation JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(presentation) = 'object'),
    interaction_state JSONB NOT NULL DEFAULT '{}'::jsonb
        CHECK (jsonb_typeof(interaction_state) = 'object'),
    reason_code TEXT COLLATE "C",
    generated_at TIMESTAMPTZ,
    invalidated_at TIMESTAMPTZ,
    invalidation_reason TEXT COLLATE "C",
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (uid, local_date, contract_version),
    CONSTRAINT ella_today_cards_ready_shape_check CHECK (
        state <> 'ready'
        OR (
            kind IN ('recap', 'memory', 'interest')
            AND content IS NOT NULL
            AND jsonb_array_length(source_refs) > 0
            AND evidence_hash IS NOT NULL
            AND generated_at IS NOT NULL
        )
    ),
    CONSTRAINT ella_today_cards_new_user_shape_check CHECK (
        state <> 'new_user'
        OR (
            kind = 'welcome'
            AND content IS NOT NULL
            AND jsonb_array_length(source_refs) = 0
        )
    )
);

CREATE INDEX IF NOT EXISTS ella_today_cards_uid_updated_idx
    ON ella_today_cards (uid, updated_at DESC);

CREATE INDEX IF NOT EXISTS ella_today_cards_source_refs_idx
    ON ella_today_cards USING GIN (source_refs jsonb_path_ops);

CREATE INDEX IF NOT EXISTS ella_today_cards_invalidated_idx
    ON ella_today_cards (invalidated_at)
    WHERE invalidated_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS ella_today_card_source_tombstones (
    uid TEXT COLLATE "C" NOT NULL,
    source_id TEXT COLLATE "C" NOT NULL,
    reason TEXT COLLATE "C" NOT NULL CHECK (reason IN ('source_deleted')),
    deleted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (uid, source_id)
);

CREATE TABLE IF NOT EXISTS ella_today_card_feedback (
    feedback_id UUID PRIMARY KEY,
    card_id UUID NOT NULL REFERENCES ella_today_cards(card_id) ON DELETE CASCADE,
    uid TEXT COLLATE "C" NOT NULL,
    expected_version INTEGER NOT NULL CHECK (expected_version >= 1),
    action TEXT COLLATE "C" NOT NULL
        CHECK (action IN ('helpful', 'not_relevant', 'hide', 'less_like_this')),
    source_fingerprint TEXT COLLATE "C",
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (uid, card_id, feedback_id)
);

CREATE INDEX IF NOT EXISTS ella_today_card_feedback_uid_created_idx
    ON ella_today_card_feedback (uid, created_at DESC);

COMMIT;
