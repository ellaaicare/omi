# Ella Managed-Cloud Consent v6

Status: draft for legal, backend, security, and App Review review. Do not publish
or copy into App Store Connect until the deployed processor inventory and vendor
terms are independently verified.

Issue: https://github.com/ellaaicare/ella-ai/issues/1123

## Exact client contract

The iOS client accepts only all of the following values:

- Policy version: `ai-data-processors-v6`
- Processor-set hash:
  `sha256:dd84e4a9da1166cff66e5de55c2570d0496a2c89d46ca431530e993758616296`
- Scope version: `managed-cloud-internal-pilot-v1`
- Scope hash:
  `sha256:727b1db818ce79090a02279f1cc6d15dfc3d65a58592b13fbed53ad048c38a30`
- Opaque server receipt prefix: `aicr_`
- A non-empty opaque `profile_binding_id`
- A parseable server-owned `server_decided_at`
- The authenticated Firebase UID as `subject_uid`; the client never submits a UID

The v6 policy response must add `scope_version`, `scope_hash`, and
`canonical_scope` to the v5 policy response. Grant, decline, and revoke
submissions add `scope_version` and `scope_hash`. Authenticated status and
receipt responses add those fields plus `profile_binding_id` and
`server_decided_at`.

Any missing field, v5 response, changed processor hash, changed runtime/model/
memory/Photon scope, changed profile binding, changed account, expired
verification, revoked decision, or unavailable authority fails closed. A changed
scope requires a new explicit grant; it is not a silent refresh.

## Managed-cloud disclosure inventory

| Legal recipient | Purpose | Data categories |
| --- | --- | --- |
| Nous Research / Hermes Cloud | Managed Ella agent runtime and session continuity | Prompt policy, messages, voice/transcript text, selected first-party context, session metadata, and model/tool usage |
| OpenAI | Model processing through the approved `openai-codex/gpt-5.6-terra` OAuth-backed route | Model input and output required to generate the response |
| Honcho Cloud | Profile-isolated derived memory and recall | Consented derived memory, selected context, and memory relationships for the bound profile |
| Photon | Narrow test/shared-line iMessage delivery | Message content and messaging identifiers for one explicitly allowed test contact |

The first-party Ella Cloudflare/Vultr control plane remains the authority for
identity, consent, entitlement, canonical events, corrections, usage, and kill
switches. It derives the profile binding and never returns vendor credentials to
iOS.

The bundled v6 manifest also retains every processor still routable under v5:
Deepgram, Soniox, Speechmatics, Google Firebase, Ella self-hosted Hermes/Honcho/
voice synthesis, OpenRouter, Google Gemini, OpenAI live voice, Groq, xAI Grok,
Inworld AI, and ElevenLabs. A release inventory must remove any route that is no
longer reachable rather than over-disclose it as active.

Photon scope is restricted to the shared test line and an explicit single test
contact. `allow_all`, caregiver delivery, and inbound attachments are false.
Changing any of those values requires a new scope hash and re-consent.

## Draft Privacy Policy addition

> **Managed cloud AI and messaging.** If you explicitly choose Allow in the
> Ella app, Ella may send the data needed for the feature you choose through
> Ella's secure first-party control plane to named service providers. Nous
> Research / Hermes Cloud may process messages, voice or transcript text,
> selected Ella context, session metadata, prompt policy, and model/tool usage
> to run the managed Ella agent. OpenAI may process model input and output
> through Ella's approved OAuth-backed model route to generate responses.
> Honcho Cloud may process consented derived memory and selected context for the
> profile bound to your Ella account so Ella can recall relevant information;
> Ella's canonical records remain in Ella's first-party system. Photon may
> process iMessage content and the messaging identifiers required to deliver
> messages for the explicitly enabled contact/channel scope.
>
> Ella does not send this content to those processors until you choose Allow.
> Choosing Not now keeps the related cloud AI, memory, voice, and messaging
> features off. You can review or revoke permission in Settings and request
> account/data deletion. Permission is versioned for the disclosed processor
> and routing scope; a material provider, model route, profile, or messaging
> scope change requires permission again.

Before publication, legal/vendor review must insert verified retention,
deletion, export, data-location, subprocessors, and contact terms for Nous
Research, OpenAI, Honcho Cloud, and Photon. Honcho's currently reported
90-day content/log/backup posture is a launch question, not an approved final
claim. The policy must also retain accurate disclosures for every active
fallback processor.

## Draft App Privacy changes

These are proposed additions for the managed-cloud path, not a replacement for a
full app-wide App Privacy audit:

| Apple category | Linked to user | Tracking | Purpose |
| --- | --- | --- | --- |
| User Content - Emails or Text Messages | Yes | No | App Functionality |
| User Content - Audio Data | Yes | No | App Functionality |
| User Content - Other User Content | Yes | No | App Functionality |
| Identifiers - User ID | Yes | No | App Functionality |
| Usage Data - Product Interaction | Yes | No | App Functionality and service reliability |

The App Privacy questionnaire must reflect retention and use across Ella's
first-party control plane and all active processors. Do not mark data as
"not collected" merely because a processor receives it transiently. Do not
declare tracking unless the deployed SDK/vendor behavior meets Apple's tracking
definition; verify rather than infer.

## Draft App Review notes

> Build: `[insert exact reviewed build]`
>
> Ella uses explicit first-use permission before sending personal content to
> third-party AI or messaging processors. To review: sign in with the supplied
> review account, choose Chat, Voice, recording, memory interaction, or the
> managed messaging entry point, and the non-dismissible "Choose how Ella uses
> cloud AI" sheet appears before the request starts. The sheet names Nous
> Research / Hermes Cloud, OpenAI, Honcho Cloud, and Photon, explains the data
> and purpose for each, identifies the restricted Photon scope, links the
> Privacy Policy, and provides Allow and Not now. Not now sends no protected
> payload and leaves those features off.
>
> Settings > Listening and consent shows whether an exact server-verified v6
> receipt is active for the signed-in account/profile. Reviewers can revoke
> permission or request account/data deletion there. Revocation stops active
> audio/voice sessions and blocks subsequent protected requests. Material
> provider, model, profile, or messaging-scope changes require consent again.
>
> The app calls only Ella's first-party API. Vendor credentials and raw profile
> identifiers are never returned to iOS. Provide the exact backend revision,
> consent policy/hash, health window, test account, and hardware-independent
> steps in the final notes.

Do not include credentials, personal contact identifiers, vendor secrets, or
family-account data in App Review notes.

## Release evidence still required

- Reviewed backend v6 implementation and migration.
- Deployed immutable backend revision with enforcement initially off.
- Synthetic authenticated policy/grant/decline/revoke/profile/scope/delete
  receipts and protected-route canaries.
- First-use, Not now, allow, revoke, account/profile switch, and scope-change
  screenshots from the exact candidate build.
- Verified live Privacy Policy and App Privacy diff.
- Stable reviewer access and backend health receipt covering the review window.
- Independent legal/security review of vendor retention, deletion, location,
  subprocessors, and contractual terms.

No real managed-cloud or Photon content is authorized by v5.
