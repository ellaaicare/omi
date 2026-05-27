# OMI Search Indexing Contract

OMI recall must not depend only on Pinecone semantic search. Production search
paths should resolve in this order:

1. Canonical timeline / exact-date lookup for recent OMI events.
2. Firestore exact/date lookup over `users/{uid}/conversations`.
3. Pinecone semantic search as a helper for fuzzy concepts.
4. Legacy workspace mirrors only as explicit fallback.

## Required Firestore Index

The conversation exact/date pass queries:

```text
users/{uid}/conversations
  WHERE discarded == false
  WHERE created_at >= <optional start>
  WHERE created_at <= <optional end>
  ORDER BY created_at DESC
```

Deploy the repo-managed index from the repository root:

```bash
firebase deploy --only firestore:indexes
```

Index definition lives in [`firestore.indexes.json`](../../../firestore.indexes.json).

## Vector Coverage Backfill

Pinecone conversation vectors live in namespace `ns1` with stable ids:

```text
{uid}-{conversation_id}
```

Reruns are idempotent because upserts replace the same vector id.

Dry-run a user/date window:

```bash
cd backend
python scripts/backfill_conversation_vectors.py \
  --uid <uid> \
  --start-date 2026-05-01T00:00:00Z \
  --end-date 2026-05-28T00:00:00Z \
  --dry-run
```

Backfill only missing vectors and fail if coverage remains below 95%:

```bash
cd backend
python scripts/backfill_conversation_vectors.py \
  --uid <uid> \
  --start-date 2026-05-01T00:00:00Z \
  --only-missing \
  --min-coverage 0.95
```

Coverage-only audit:

```bash
cd backend
python scripts/backfill_conversation_vectors.py \
  --uid <uid> \
  --start-date 2026-05-01T00:00:00Z \
  --coverage-only \
  --min-coverage 0.95
```

The script logs:

- `conversation_vector_coverage`
- `conversation_vector_backfill_failed`
- `conversation_vector_coverage_below_threshold`
- `conversation_vector_backfill_summary`

These log lines are intended for runtime alerting. A nonzero exit code means
backfill errors occurred or the requested `--min-coverage` threshold was not
met.

## Search Path Audit

Known production consumers:

- `/v1/voice/search`: OMI-scoped searches call canonical timeline first, then
  Firestore legacy OMI fallback. Results include provenance metadata such as
  `canonical_event`, `firestore_legacy_omi`, and `hermes_voice_memory`.
- MCP Plato-Hermes tools: recent context and memory search use canonical
  timeline first, then OMI Firestore fallback, then workspace search fallback.
- LangChain retrieval `search_conversations_tool`: now performs Firestore
  exact/date lookup first and merges Pinecone vector results after it.

New consumers should expose provenance in responses and should not treat an
empty Pinecone result as proof that no OMI conversation exists.
