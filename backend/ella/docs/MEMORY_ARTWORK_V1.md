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
  The default-off startup worker consumes pending records, and an interrupted
  process can reclaim the generation after its bounded lease expires.
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
  exact current consent, style, binding, profile, and authority before issuing a
  five-minute signed first-party URL.

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
```

When approved, the operator also supplies:

```text
ELLA_MEMORY_ARTWORK_BUCKET=<private first-party bucket name>
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

## Backfill and rollback

Backfill scans a bounded recent window and queues at most ten terminal enriched
conversations. Repeating it reuses the generation identity and does not create
additional work. It performs no provider call in the request.

Rollback is configuration-only while no objects have been generated: set all
four gates false. After controlled generation, memory deletion removes the exact
object and deterministic memory prefix before deleting the conversation. The
service records a durable owner-level cleanup requirement before every upload,
so account deletion removes the owner's private prefix before Firestore/Firebase
cleanup and returns typed HTTP 503 when bucket configuration is absent but
storage use cannot be disproved. Exact-UID dispatch records are removed only
after storage cleanup succeeds. A storage deletion failure aborts deletion
instead of leaving an object behind while reporting success.

Before a real release, record evidence for:

1. in-app consent naming the actual processor/routing layer and purpose;
2. Privacy Policy, App Privacy, and App Review text matching retention/caching;
3. private bucket policy and signed-URL expiration;
4. exact service credential ownership/mode and worker subject binding;
5. dry-run counts for one owner, with no payload or user content in logs;
6. deletion of a generated object and an idempotent newest-ten replay.
