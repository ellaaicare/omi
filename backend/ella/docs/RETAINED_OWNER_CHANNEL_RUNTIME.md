# Retained owner channel runtime

This contract describes the default-off path for routing the configured
retained owner's built-in chat and voice support to the same physical Hermes
runtime used by verified iMessage. It does not authorize deployment or
activation. Those gates are tracked in `ellaaicare/ella-ai#1259`.

## Authority

`ELLA_RETAINED_OWNER_CHANNEL_RUNTIME_ENABLED=true` is considered only when the
authenticated subject exactly matches `ELLA_PLATO_UID`. The backend then
resolves the active, healthy, targetless `role=imessage` runtime binding. A
missing, disabled, unhealthy, target-bearing, wrong-provider, or wrong-role
binding fails closed. The resolved profile and runtime agent must both be
exactly `plato-eval`; the full authority digest additionally binds the endpoint,
credential, workspace, ownership coordinates, revision, and policy receipt.
The selector never falls through to `role=user`, a legacy agent registry, a
global profile, or a caller-selected runtime.

The ordinary user binding is neither updated nor deleted. The role name remains
`imessage` for compatibility with migration 023 and the reviewed enrollment
graph; no migration is required. Migrations 022 and 023 must not be rerun as
part of enabling or rolling back this feature.

## Session and memory semantics

The preserved runtime/profile and Honcho authority can be shared safely, but a
single mutable Hermes session cannot. The pinned Hermes API server states that
it has no per-session lock and concurrent turns on one session are
last-writer-wins. Therefore the backend uses:

```text
memory key:       ella:omi:{uid}:canonical
app chat session: ella:omi:{uid}:canonical:channel:ios-chat
iMessage session: ella:omi:{uid}:canonical:channel:imessage
voice tool turn:  the signed voice session id
```

The canonical event ledger is the cross-channel history authority. App chat
injects recent canonical events before inference and all supported channels
write owner-scoped canonical events. Channel-specific Hermes session history is
an optimization, not proof of shared history. This contract does not combine
the ordinary profile's session database or Honcho workspace with the retained
profile.

## Voice semantics

With the gate enabled, voice session issuance, context, search, and
`ask_ella` tool execution revalidate the exact retained runtime digest. The
realtime speech model remains the configured Grok V2V provider. The retained
Hermes runtime supplies its workspace/Honcho context and handles `ask_ella`;
this does not claim that every spoken sentence is authored by Hermes.

## Activation and rollback

Before activation, require all of the following:

1. independent review of the exact source and deployment artifact;
2. an active healthy retained `role=imessage` binding that resolves to the
   preserved runtime and no other owner;
3. negative tests proving non-owner subjects and absent/drifted roles cannot
   select the retained runtime;
4. authenticated chat and voice smoke tests showing the retained digest is
   revalidated before provider/runtime work;
5. Photon project/registrar health and genuine owner enrollment proof under the
   existing iMessage contract.

Rollback is configuration-only: set
`ELLA_RETAINED_OWNER_CHANNEL_RUNTIME_ENABLED=false` and reload the backend.
New chat and voice sessions resume the unchanged ordinary authority. Any
in-flight retained request fails its digest/role revalidation instead of
falling through. The retained binding, ordinary binding, canonical events,
Honcho data, iMessage enrollment, migrations, and provider registrations are
not rewritten by rollback.
