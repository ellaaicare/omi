# Generated memory and Daily Note images

Status: foundation only. No provider adapter, worker, upload, delivery route, or production consent policy is enabled.

## Existing boundaries

- Conversation enrichment runs in `ella/services/hermes_cloud_enrichment.py`. It reads the exact owner-bound conversation and authoritative transcript, creates a summary, and independently verifies that summary against transcript excerpts.
- `ella/services/summary_writeback.py` binds the transcript hash, summary hash, owner hash, conversation hash, request identities, and new active summary version before the conversation becomes canonical. Stale transcript or summary-version writes fail closed.
- `ella/services/today_card_postgres.py` admits only active, grounded summary versions into `ella.today_card.v1`. The materializer stores source refs, evidence hash, and source watermark; reads re-check that those refs are still current.
- The mobile memory journal renders `ServerConversation` records. Its release-line media order is an existing source photo first and app-owned static watercolor artwork otherwise. Generated imagery must remain below source photos in that order.
- Daily Note presentation is the `TodayCardPresentation` JSON object. The app already treats absent presentation details as normal, so `background_image` is additive.
- Existing AI consent is processor-set, scope, profile-binding, and authority-generation bound. Its current processor inventory does not authorize an image processor. It must not be reused as implicit image-generation consent.
- Canonical conversation writeback and Today Card persistence are the factual authorities. Provider responses, private storage objects, and UI caches are not canonical receipts.

## Additive contract

`models/generated_image.py` defines the only asset reference that app-facing memory or Today Card records may carry. It contains a first-party authenticated delivery path, never a provider URL or private storage key. It is valid only for an approved moderation result and a canonical-confirmed receipt.

`ella/services/generated_images.py` defines:

- an exact owner/profile/authority-generation snapshot;
- a memory subject (conversation id plus optional fact-memory id) or Daily Note subject (Today Card id);
- source version, source digest, grounding-receipt digest, and current generation;
- prompt contract version and prompt digest without persisted prompt content;
- named provider, named legal processor, and exact model;
- image-specific consent policy, processor-set, scope, and immutable receipt reference;
- provider generation request id, private asset id/digest, moderation result, and canonical event id;
- job states and pure admission/start/output/canonical-confirmation transitions.

Admission fails closed if consent is missing, declined, revoked, expired, future-dated, or differs in owner, binding, authority generation, provider, processor name/id, model, policy, processor set, or scope. The exact consent receipt is checked again immediately before provider egress, so a queued job cannot run after revocation or under a replacement grant. Admission and egress also fail when the grounded source snapshot or generation is no longer current. Codex image generation identifiers are explicitly rejected as production providers.

The SQL ledger in migration 017 preserves those bindings independently of Firestore and Today Card JSON. A future worker must use compare-and-set updates for every state transition; the pure functions in the foundation repeat the source/generation checks immediately before provider egress, output binding, and attachment.

## Intended sequence

1. Enrichment or Today Card materialization produces a current grounded source receipt.
2. The app presents a separate first-use image-sharing disclosure naming the actual image processor and offers a visible decline/not-now path.
3. The server reads the immutable image-specific consent receipt and current owner authority. The client cannot supply authority, source text, prompt text, processor, or model.
4. A server-side prompt builder derives a minimized creative brief from the grounded source. Only its contract version and digest are persisted.
5. Admission and the pre-egress CAS both pass; only then may a server-side provider adapter receive the brief.
6. Provider bytes are copied into private first-party storage, digested, discarded from memory, moderated, and assigned alt text. Provider URLs are not retained.
7. A content-free receipt is written to the canonical ledger. A final CAS confirms the source version and generation, then attaches the approved asset reference.
8. Memory UI continues source photo -> approved generated image -> static fallback. Daily Note continues approved background -> existing botanical/static surface. Any parse, auth, moderation, receipt, delivery, or digest failure falls back without hiding the text.

## Deliberately unresolved

- legal processor/provider and model;
- the exact disclosure copy, image data categories, retention/deletion terms, and consent policy/scope versions;
- whether generation runs in a first-party Worker or an authenticated n8n-mediated worker;
- private object store/bucket, retention TTL, deletion propagation, and authenticated delivery route;
- prompt-builder and moderation implementations;
- quotas, retry policy, cost controls, and whether memory images are automatic or user-triggered;
- release-line UI integration and visual treatment.

Until those choices are approved and the image-specific consent policy is shipped, no job can obtain a valid production consent grant and no user content may leave Ella for image generation.
