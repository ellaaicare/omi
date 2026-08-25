# Ella Memory Artwork v1

This contract adds private generated artwork to the conversation-backed Ella
memory gallery. It is source-only and disabled by default. It does not authorize
provider setup, storage mutation, deployment, consent release, or a real-data
backfill.

## Authority and lifecycle

- The Firebase bearer establishes the user. No user-facing route accepts a UID.
- The active Hermes runtime binding establishes the account/profile scope.
- The generation identity is SHA-256 over the UID, binding/profile, conversation
  id, active enriched-summary revision, style version, and prompt hash.
- A conversation is eligible only after terminal text enrichment
  (`writeback_applied` with an enriched summary kind). Artwork failure never
  changes the text-enrichment result. Reservation atomically writes both the
  conversation generation state and a content-free durable dispatch record.
  The default-off startup worker durably claims each record before provider
  work, and an interrupted process can reclaim the generation after its bounded
  lease expires. Terminal worker updates require the same claim and never
  recreate a removed dispatch record. The internal single-memory processing
  route uses this same claim path; provider/storage execution cannot bypass it.
- Provider calls require current image-specific consent and matching runtime
  authority immediately before egress. Consent, style, source, and authority are
  checked again before object storage and final writeback.
- Caregiver-private, safety, distress, emergency, self-harm, Guardian-relevant,
  and elevated-risk sources are excluded from provider egress.
- Public conversation responses expose only `artwork` status, style/revision,
  dimensions, content type, failure code, and update time. Object keys, provider
  credentials, authority digests, and binding identifiers remain private.
- Ready images are fetched through
  `GET /v1/ella/memories/{memory_id}/artwork`, which revalidates the owner and
  exact current consent, style, binding, profile, authority, and sensitive-source
  exclusion before issuing a five-minute signed first-party URL. Both the feature
  and release gates are serving kill switches; release-off never signs an existing
  object.

## API

| Method | Path | Authority | Purpose |
| --- | --- | --- | --- |
| GET | `/v1/ella/memory-artwork/preferences` | Firebase owner | Read consent/style state and supported versions. |
| PUT | `/v1/ella/memory-artwork/preferences` | Firebase owner | Persist versioned accept/decline and style choice for the current binding. |
| GET | `/v1/ella/memories/{memory_id}/artwork` | Firebase owner | Read typed state or an owner-scoped signed URL. |
| POST | `/v1/ella/memories/{memory_id}/artwork` | Firebase owner | Idempotently queue one terminal enriched memory. |
| POST | `/v1/ella/memory-artwork/backfill` | Firebase owner | Queue at most the newest ten eligible memories. |
| POST | `/v1/ella/internal/memory-artwork/{memory_id}/process` | Exact service key and bound subject | Claim and process one queued generation. |

Old clients remain compatible because `artwork` is optional. Declined and
unavailable states do not contain a URL; clients must render a local neutral
fallback rather than a generic image presented as memory-specific.

## Configuration

All booleans default to `false` and must remain false until consent, privacy,
processor disclosures, storage, and a release review are complete.

```text
ELLA_MEMORY_ARTWORK_ENABLED=false
ELLA_MEMORY_ARTWORK_RELEASE_ENABLED=false
ELLA_MEMORY_ARTWORK_PROVIDER_ENABLED=false
ELLA_MEMORY_ARTWORK_BACKFILL_ENABLED=false
ELLA_MEMORY_ARTWORK_INTERNAL_OWNER_UIDS=
```

When approved, the operator also supplies:

```text
ELLA_MEMORY_ARTWORK_BUCKET=<private first-party bucket name>
ELLA_MEMORY_ARTWORK_PROVIDER=first_party_adapter
ELLA_MEMORY_ARTWORK_INTERNAL_OWNER_UIDS=<exact internal Firebase UID allowlist>
```

The internal-owner release uses the fixed first-party adapter below. The direct
xAI adapter remains dormant compatibility code and is not part of consent v10
or the approved release configuration.

The alternate fixed first-party adapter uses:

```text
ELLA_MEMORY_ARTWORK_PROVIDER=first_party_adapter
ELLA_MEMORY_ARTWORK_PROVIDER_URL=<fixed first-party adapter URL>
ELLA_MEMORY_ARTWORK_PROVIDER_ALLOWED_HOST=<exact adapter host>
ELLA_MEMORY_ARTWORK_PROVIDER_TOKEN_FILE=<root/service-owned 0600 token path>
ELLA_MEMORY_ARTWORK_SERVICE_KEY=<dedicated worker service credential>
ELLA_MEMORY_ARTWORK_WORKER_INTERVAL_SECONDS=5
```

The provider URL cannot be selected by a request or memory payload. Plain HTTP
is accepted only for loopback. Redirects and ambient proxy variables are
disabled. The adapter must return raw PNG, WebP, or JPEG bytes with
`X-Ella-Image-Width` and `X-Ella-Image-Height`; vendor-temporary URLs are not
accepted or persisted.

The request contract is `ella.artwork.service.v1`. The backend sends a bounded
`ella.artwork.brief.v1` object containing the exact owner UID, opaque profile
binding, authority generation, enriched-summary revision, consent version,
selected style, title, and overview. It does not send raw audio, source photos,
transcripts, a full history, a caller-selected URL, or an opaque instruction
prompt. The first-party designer may enrich this brief with separately reviewed,
owner-scoped mood and linked-memory context later, but must never infer or fetch
those fields through an unbounded agent tool.

`ELLA_MEMORY_ARTWORK_INTERNAL_OWNER_UIDS` is mandatory for environment-derived
configuration. An empty allowlist or a non-matching Firebase UID disables every
preference, enqueue, worker, signed-read, and backfill path even when the four
rollout flags are true. This protects the owner-funded Codex OAuth route from
external or cross-account use.

## Backfill and rollback

Backfill scans a bounded recent window and queues at most ten terminal enriched
conversations. Repeating it reuses the generation identity and does not create
additional work. It performs no provider call in the request.

Rollback is configuration-only while no objects have been generated: set all
four gates false. After controlled generation, memory deletion removes the exact
object or the binding-scoped deterministic memory prefix before deleting the
conversation; it never falls back to an owner-wide listing for a missing binding.
The service records a durable owner-level cleanup requirement before each upload
under an active worker claim. The marker remains conservative when an upload
outcome is uncertain, so account deletion removes the owner's private prefix
before Firestore/Firebase cleanup and returns typed HTTP 503 when bucket
configuration is absent but storage use cannot be disproved. Account deletion
first writes an owner marker that prevents new dispatch claims. A claimed worker
keeps deletion at typed HTTP 503 until it reaches a durable terminal state; only
then are storage and exact-UID dispatch records removed. A storage deletion or
worker-drain failure aborts deletion instead of leaving an object behind while
reporting success.

Before a real release, record evidence for:

1. in-app consent naming the actual processor/routing layer and purpose;
2. Privacy Policy, App Privacy, and App Review text matching retention/caching;
3. private bucket policy and signed-URL expiration;
4. exact service credential ownership/mode and worker subject binding;
5. dry-run counts for one owner, with no payload or user content in logs;
6. deletion of a generated object and an idempotent newest-ten replay.
