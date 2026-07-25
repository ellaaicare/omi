-- Durable, owner-scoped post-session memory reinterpretation queue.
--
-- Apply before enabling ELLA_MEMORY_REINTERPRETATION_ENABLED:
--   psql "$ELLA_POSTGRES_DSN" \
--     -f backend/migrations/007_create_memory_reinterpretation_outbox.sql

CREATE TABLE IF NOT EXISTS memory_reinterpretation_jobs (
    id TEXT PRIMARY KEY,
    uid TEXT COLLATE "C" NOT NULL,
    logical_session_id TEXT COLLATE "C" NOT NULL,
    conversation_id TEXT COLLATE "C" NOT NULL,
    starting_summary_version_id TEXT COLLATE "C" NOT NULL,
    source_identity TEXT NOT NULL,
    canonical_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    transcript_hash TEXT NOT NULL,
    transcript_revision INTEGER NOT NULL DEFAULT 1
        CHECK (transcript_revision >= 1),
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN (
            'pending',
            'running',
            'retry',
            'no_change',
            'pending_review',
            'applied',
            'conflict',
            'dead_letter'
        )),
    outcome TEXT,
    proposal_plan JSONB,
    progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    proposal_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    correction_ids JSONB NOT NULL DEFAULT '[]'::jsonb,
    receipt_refs JSONB NOT NULL DEFAULT '[]'::jsonb,
    not_before TIMESTAMPTZ NOT NULL,
    lease_owner TEXT,
    lease_token TEXT,
    lease_expires_at TIMESTAMPTZ,
    attempt_count INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    last_error_code TEXT,
    last_error_detail TEXT,
    completed_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (
        uid,
        logical_session_id,
        conversation_id,
        starting_summary_version_id
    )
);

CREATE INDEX IF NOT EXISTS memory_reinterpretation_jobs_due_idx
    ON memory_reinterpretation_jobs (not_before, created_at)
    WHERE status IN ('pending', 'retry');

CREATE INDEX IF NOT EXISTS memory_reinterpretation_jobs_expired_lease_idx
    ON memory_reinterpretation_jobs (lease_expires_at)
    WHERE status = 'running';

CREATE INDEX IF NOT EXISTS memory_reinterpretation_jobs_uid_conversation_idx
    ON memory_reinterpretation_jobs (uid, conversation_id, created_at DESC);

ALTER TABLE memory_reinterpretation_jobs
    ADD COLUMN IF NOT EXISTS proposal_plan JSONB,
    ADD COLUMN IF NOT EXISTS progress JSONB NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS transcript_revision INTEGER NOT NULL DEFAULT 1;

CREATE TABLE IF NOT EXISTS memory_reinterpretation_attempts (
    id BIGSERIAL PRIMARY KEY,
    job_id TEXT NOT NULL REFERENCES memory_reinterpretation_jobs(id) ON DELETE CASCADE,
    transcript_revision INTEGER NOT NULL DEFAULT 1,
    attempt_number INTEGER NOT NULL,
    lease_token TEXT NOT NULL,
    worker_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    error_code TEXT,
    metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    finished_at TIMESTAMPTZ
);

ALTER TABLE memory_reinterpretation_attempts
    ADD COLUMN IF NOT EXISTS transcript_revision INTEGER NOT NULL DEFAULT 1;

ALTER TABLE memory_reinterpretation_attempts
    DROP CONSTRAINT IF EXISTS memory_reinterpretation_attempts_job_id_attempt_number_key;

CREATE UNIQUE INDEX IF NOT EXISTS memory_reinterpretation_attempts_generation_attempt_idx
    ON memory_reinterpretation_attempts (
        job_id,
        transcript_revision,
        attempt_number
    );

CREATE INDEX IF NOT EXISTS memory_reinterpretation_attempts_job_idx
    ON memory_reinterpretation_attempts (job_id, attempt_number DESC);
