# Authenticated Dream Media

This backend contract implements the accepted Ella decision in
`ellaaicare/ella-ai/docs/decisions/2026-09-25-authenticated-dream-media.md`.
Dream media is private user data. It is never served from a public bucket,
persisted as a signed URL, or rendered from stored HTML.

## Client API

All client routes require an exact Firebase bearer subject.

- `GET /v1/ella/dreams` returns structured dream metadata. It never returns
  object keys or signed URLs.
- `GET /v1/ella/dreams/{dream_id}/media` loads the record under the caller's
  UID and returns a fresh signed URL for each committed asset. Missing dreams
  and dreams owned by another UID both return the same `404` response.
- `DELETE /v1/ella/dreams/{dream_id}` deletes every inventoried object, runs
  the user orphan pass, and tombstones the record before acknowledging.

Responses use `Cache-Control: private, no-store`, `X-Robots-Tag: noindex`,
`Referrer-Policy: no-referrer`, and `X-Content-Type-Options: nosniff`.
Images and audio use a 300-second signed URL. Video uses 900 seconds.

## Pipeline Upload API

The pipeline calls:

```text
POST /v1/ella/internal/dreams/{dream_id}/media
X-Ella-Dream-Pipeline-Key: <service credential>
X-Ella-Subject-Uid: <exact Firebase uid assigned by the trusted pipeline>
Content-Type: multipart/form-data
```

The service credential comes from `ELLA_DREAM_PIPELINE_SERVICE_KEY`. The
caller cannot select an unbound UID through JSON or query parameters.

Multipart fields:

- `metadata`: JSON object with `request_id` (8-128 safe ID characters),
  optional `title`, `narrative`, `captions[]`, `source_memory_ids[]`, and
  `created_at`.
- `file`: one media file, at most 64 MiB.

Accepted byte signatures are `video/mp4`, `image/png`, `image/jpeg`,
`image/webp`, `audio/mpeg`, and `audio/ogg`. The supplied filename and MIME
header are not authoritative. MP4 requires `moov` before `mdat`. HTML, SVG,
Markdown, JavaScript, PDF, and unknown bytes are rejected before inventory or
upload.

`request_id` is the idempotency key within a dream. The success response is:

```json
{
  "dream_id": "opaque-dream-id",
  "asset": {
    "asset_key": "opaque-random-key",
    "state": "committed",
    "content_type": "image/webp",
    "pepper_version": 1,
    "created_at": "...",
    "committed_at": "...",
    "bytes": 1234
  }
}
```

The response does not expose `object_key`, SHA-256, a bucket name, or a signed
URL. A retryable storage or commit failure returns `503`; validation and
content failures are non-retryable. If upload succeeds but commit fails, the
pending inventory entry remains the deletion authority.

## Configuration

Only configuration names belong in source control:

- `BUCKET_DREAM_MEDIA`
- `ELLA_DREAM_PIPELINE_SERVICE_KEY`
- `DREAM_MEDIA_ACTIVE_PEPPER_VERSION` (for example, `1` or `v1`)
- `DREAM_MEDIA_PEPPER_VERSIONS` (comma-separated non-secret registry of every
  retained or retired version whose prefix must still be swept)
- `DREAM_MEDIA_KEY_PEPPER_V1`, `DREAM_MEDIA_KEY_PEPPER_V2`, and later numbered
  secret values while retained (at least 32 UTF-8 bytes of secret material)
- `DREAM_MEDIA_SWEEP_INTERVAL_SECONDS` (optional; minimum 300, default one day)

Never print, log, commit, or copy the values. Removing a pepper value does not
remove its version from `DREAM_MEDIA_PEPPER_VERSIONS`.

## GCP Infrastructure Checklist

An authorized infrastructure owner must complete and independently verify all
of these steps. This source change does not create or mutate vendor resources.

- Create the bucket named by `BUCKET_DREAM_MEDIA` with public access
  prevention **enforced**, uniform bucket-level access, and no public ACL.
- Do not attach a CDN, load balancer, website configuration, or public origin.
- Grant the backend service account object create/read/delete/list only on
  this bucket, typically bucket-scoped `roles/storage.objectAdmin`.
- Grant the runtime identity the narrow service-account signing permission
  needed for GCS v4 URLs (`iam.serviceAccounts.signBlob`, typically
  `roles/iam.serviceAccountTokenCreator` on the signer service account).
- Keep the pipeline credential and each pepper value in the production secret
  store. Record names and presence only.
- Verify an unsigned object URL is `403`, an expired signed URL is `403`, and
  the API's IDOR check signs nothing.
- Verify production logs contain neither `X-Goog-Signature` nor a storage URL
  query string.

## Inventory, Sweep, And Retirement

Object keys have this form:

```text
dreams/v1/p{version}/{HMAC-SHA256(pepper_version, uid)}/{random128}/{random128}.{ext}
```

The Firestore record is written with `state: pending` before bytes are sent to
GCS. A successful upload is then committed with byte count and SHA-256.
Pending entries older than 24 hours are deleted with their objects. The daily
sweep performs a per-user pass for retained peppers and a bucket-wide pass for
every version prefix, including retired peppers.

A pepper may be removed only after there are zero inventory references, a
successful bucket-wide pass records zero remaining objects under its prefix,
and the retirement receipt is recorded in the infrastructure registry.

Dream, source-memory, and account deletion delete stored inventory keys and
run the applicable orphan pass before acknowledging. Object deletion never
recomputes a key from a pepper.

## Composition With Memory Artwork

OMI main does not yet contain the stacked `#1281` memory-artwork files even
though that production-stack PR was merged. This change therefore introduces
`GCSPrivateObjectBucket` as the shared low-level immutable private-object
adapter and keeps dream-specific validation in `GCSPrivateMediaStore`.
When the `#1281` stack is composed for production, its artwork store should
adopt the shared adapter rather than introduce another GCS primitive. Artwork
retains its own bucket, key contract, cache policy, and erasure semantics.

## Source-Of-Truth Differences

The accepted ADR is binding. The issue checklist originally assigned bucket
and IAM mutations to the backend owner, while the implementation assignment
explicitly forbids those mutations; this delivery provides the exact checklist
instead. The assignment also makes the versioned environment contract
explicit (`DREAM_MEDIA_KEY_PEPPER_V{n}` plus an active version), so no generic
unversioned `DREAM_MEDIA_KEY_PEPPER` is read.
