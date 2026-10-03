# Standard Talk Protected Reference Foundation

Source-only store foundation based on backend `d49e3d3531`; dormant transaction
composition based on accepted backend `45ace27b96`. No production caller,
router, chat, TTS, authorization grant or runtime is changed. The optional
ledger connection/strict-insert seam leaves all legacy defaults unchanged.
This does not enable echo protection or permit deployment/provider requests.

## Store Contract

`database.standard_talk_playback_references` uses the existing asyncpg pool
factory convention. Transitions run in one transaction. Claim and completion
lock the exact owner/reference row first, then evaluate the conditional state
and database-clock expiry in a subsequent statement while holding that lock.
This also rejects deadlines that elapse naturally during an unchanged row-lock
wait; evaluating expiry in the waiting UPDATE alone is insufficient.
Every store method also accepts the caller's existing active transaction
connection, so the next integration can share authority locks and commit the
ledger and protected association atomically. A supplied nontransactional
connection is rejected; it never silently acquires a second connection.
Migration 020 creates a dedicated table, never generic canonical metadata.
PUBLIC has no table privileges. No plaintext capability, bearer, audio, text,
provider credential or client-controlled authorization metadata is stored.

Only a future server issuer may call `issue`: the random 32-byte base64url
capability is returned once; only its SHA256 is persisted. It binds the exact
owner, canonical event and source identity, raw canonical text SHA256, hashed
runtime authority snapshot, consent receipt SHA256 and normalization version.
The runtime snapshot MUST include immutable account/profile/binding and all
current authority epochs, not merely model configuration or an enabled flag.
The consent digest MUST identify the exact receipt, not its policy version.

The unique owner/event/source/runtime/consent association prevents issuance
again for the same turn, including changed text or normalization, failure,
expiry and response loss. Existing capabilities cannot be reconstructed.
Rows are tombstones; no automatic cleanup or renewal is introduced.

Database time fixes lifetime at five minutes. `claim` conditionally changes
`issued` to `synthesizing` exactly once and assigns server-only random claim
and playback IDs. `publish` requires the same claim, exact association and
unexpired row. `fail` retires that claim; `revoke` irreversibly retires an
exact association. Late completions and duplicate claims cannot renew a row.

## Authority Integration Remains A Gate

Store comparisons are not authentication or a current-authority resolver.
There are NO production callers. Next reviewed integration must read back
the exact server-written assistant event and compare raw text to the actual
provider result before issuance. It must acquire the existing owner/runtime
authority locks, resolve current binding and consent under those locks, and
check the original immutable epochs before claim and after synthesis.
Revoke/rebind ABA must never reuse an old authority digest. Revoke must
retire the protected row or publication must observe a new epoch; a boolean
grant or caller-constructed dataclass does not establish this condition.

The future generated-ledger publication must be transactional with this
protected association, or remain denied. This foundation's `publish` does
not write a ledger or imply audio was generated, delivered or played.
No response-loss retry, regeneration, byte replay or new audio candidate is
supported. Exact authenticated actual-start/completion/failure receipts are
separate future integration, never synthesis or queued/fetched evidence.

## Proposed Spoken Text Contract (Not Routed)

`standard-talk-utf16-500-emoji-v1` is a proposed shared-vector contract:

1. Preserve raw canonical text for SHA256 binding; separately trim the spoken
   input using Dart String.trim semantics (the current Standard Talk reply is
   trimmed before its 500-unit bound).
2. Take the first 500 UTF16 code units, matching Dart substring rather than
   Python code-point slicing. Remove emoji using the exact character ranges
   in release986 `ella_voice_chat_page.dart:_emojiRegex`, including selectors,
   joiners and emoji tags. Collapse consecutive ASCII spaces and trim again.
3. A truncation splitting a surrogate pair can leave a lone surrogate under
   the existing Dart code. The proposed server contract rejects that result
   as `invalid_unicode`, rather than silently changing text or sending invalid
   UTF8. This edge requires explicit client alignment before route integration;
   it is NOT represented as already identical current-client behavior.
4. Empty spoken output is denied. Do not substitute caller text or fallback
   content; mixed live speech/echo classifiers are unchanged.

Synthetic vectors are in `standard_talk_spoken_vectors.json`; they are shared
fixtures for future Dart and Python implementation, not a new normalizer here.
They cover astral boundaries, emoji removal, combining marks and space handling.

## Proof And Release Gates

Unit tests prove SQL arguments, framing, stored-data minimization, default
caller absence and fail-closed comparison. Disposable PostgreSQL tests prove
actual two-pool/row-lock claims, uniqueness after response loss, database expiry,
exact tuple mismatch, failed/revoked ABA and late publication. Missing disposable
DSN means SKIPPED/NOT PROVEN, never acceptance. No live DSN or migration is used.
Full routes, canonical role/source enforcement, current authority invalidation,
transactional ledger publication, API/client compatibility and physical echo
acceptance remain separate security and physical gates.
# Dormant Transaction Composition (Source Only)

`ella.services.standard_talk_playback.StandardTalkPlaybackComposition` has no
route, chat issuer, production snapshot loader, production normalizer, provider
call, client mode, or activation flag. It defaults OFF and denies without both
trusted server dependencies. Synthetic fixtures exercise these seams; they do
not establish a live Firestore read or an installed playback contract.

The self-profile-only SQL composition reuses the shared database owner lock as
its first statement, then existing runtime policy locks and admission helpers.
It rereads actual `users.id`, binding/target/invitation lineage, active
entitlement revision, exact current consent receipt/contract, mirror revision,
and monotonic mirror epoch on that same connection. Missing/lagging mirrors
deny: there is no bootstrap, enrollment, grant mutation, delegated-coordinate
mapping or global resolver change. Managed Hermes-chat lineage is required;
retained profiles without this protected lineage remain unsupported here.

Every stage needs a fresh server runtime and current Firestore consent snapshot
outside the transaction. The future loader must verify the exact authorized
status, decision, processor IDs, and receipt; it cannot accept caller-created
authority dataclasses. PostgreSQL locks are not distributed Firestore atomicity.
Before a production caller is added, its pre/post-work freshness and exact
loader contracts need independent review.

Issuance takes only the freshly produced internal server assistant turn tuple.
It requires exact persisted UID/event/source/session/role/channel/provider,
privacy scope, scan policy and raw canonical text SHA256. Public canonical
writes can be assistant-shaped; persisted role alone is NEVER server issuance
provenance. There is no event-ID lookup route accepting arbitrary public rows.
This internal descriptor is not a public authorization proof.

Normalization remains a held dependency. The proposed vector contract is not
implemented. A future reviewed server normalizer must return the exact version,
raw canonical digest, bounded spoken text and spoken digest; arbitrary client
text is not accepted. Publication rechecks that the post-work output digest
matches the admitted one. Claim commits before any future synthesis. A fresh
snapshot and all original SQL predicates are rechecked after synthesis, then
the reference transition and generated ledger write commit together; failed
ledger writes roll back publication. Supplied-connection ledger writes do not
open another pool transaction or run retention cleanup under the owner lock.
Protected publication uses `require_new=True`: an existing or concurrently
inserted same-owner/playback row denies and rolls back the reference transition
rather than inheriting legacy upsert text. Default legacy upsert is preserved.
The new lane currently requires exact target/entitlement consent contract
lineage; v10/v11-compatible public wiring remains a separate reviewed gate.

Generated is not playback or echo evidence. Receipts and lifetime policy remain
unimplemented in this stage: original five-minute validity is required for a
first start or completion without admitted start. A later completion/failure
after an admitted start may close only that playback under still-current exact
authority epochs. Late completion without start denies; idempotency follows
authority checks; no reference renewal, synthesis retry or audio replay.
Public routes, protected receipt admission, app normalization vectors/player
integration, distributed freshness and physical mixed-live-speech acceptance
remain separate gates. No claim of installed feedback prevention is made.
