# Dormant signed summary-tool renewal

This source foundation is OFF unless `ELLA_MCP_OAUTH_REFRESH_ENABLED=true`.
It creates no MCP identity grant, runtime registration, profile attachment,
consent, or managed-profile capability. Existing `no_mcp` policy is unchanged.
Ordinary Firebase exchanges, authorization codes without `offline_access`, and
legacy static credentials retain their existing behavior.

## Admission

Only the existing first-party authorization-code endpoint may initially issue
a refresh family. The caller must explicitly request `offline_access`, use a
currently DCR-registered public client declaring authorization-code and refresh
grants, use its registered redirect URI, and prove S256 PKCE. The code records
the exact client, redirect and requested scope. HTTPS metadata client IDs and
legacy configured/static client secrets cannot select this lane.

The server resolves the verified identity's original explicit self grant. The
requested access scopes must be within that original ceiling and the current
grant; `offline_access` is an issuance marker, not a new data/write scope.
Renewable access is limited to `tools:read`, `memory:read`, `summaries:write`
and the three already-registered summary tools. Grant metadata must match the
exact current Hermes chat binding and profile. Renewal accepts no replacement
owner, UID, profile, binding, grant or tool selection. Scope changes require a
fresh authorization-code exchange. Existing summary rollout/registration
checks still apply; refresh issuance does not turn those checks on.

## Credentials And Authority

Opaque refresh secrets contain 32 random bytes. Only SHA-256 digests are stored;
no plaintext refresh/access token is stored or logged. Families expire seven
days after creation, with no sliding extension. Access expiry is at most one
hour and never later than family expiry. An optional grant expiry must be a
server datetime and is rechecked on admission.

One Firestore read transaction captures the exact grant document, account's
current consent pointer and immutable consent receipt, retaining nanosecond
document revisions. Existing consent policy validation remains authoritative.
The family also retains the server-resolved account/profile UUIDs, runtime
authority digest, client-registration digest and exact scope/tool ceilings.
Revoke/regrant, deletion/recreation, profile/binding changes and replacement
revisions invalidate the family even if visible fields later look identical.
Because consent is embedded in the user document, unrelated updates to that
document conservatively require reauthorization too; this is not a sliding
authorization or a promise of uninterrupted refresh availability.

PostgreSQL takes the shared owner advisory lock before family/token row locks,
rechecks persisted owner coordinates, and checks DB-clock expiry after waiting
for locks. Rotation consumes the old digest and inserts one replacement in a
single transaction. Reuse of a consumed digest commits revocation of the whole
family. Concurrent renewal may therefore revoke a winner's family; every
renewable access token checks family status, so no winner stays admitted after
that revocation. Wrong-client requests cannot consume or revoke another
client's family.

Runtime resolution is outside the family transaction (it owns independent
authority locks). Fresh authority is checked before and after credential
publication. Postcommit drift or failed final authority reads return no token
and attempt to revoke the family. Ambiguous commits are never automatically
replayed. A client must reauthorize rather than blindly retry a lost refresh
response. There is **no distributed atomic transaction** across Firestore and
PostgreSQL: each protected use independently checks the captured revisions,
current authority and family before and after awaited work. This is a bounded
fail-closed snapshot contract, not instantaneous global revocation across
stores or cancellation of already-committed operations.

## Protected Endpoint Coverage

Renewable JWTs carry only two new authority fields: `refresh_family_id` and
`refresh_authority_digest`. Normal/static sessions bypass renewal storage.

- Plato `POST /mcp`, `GET /mcp`, `POST /mcp/sse/message`, `DELETE /mcp`: current
  family and exact authority checked at entry.
- Each JSON-RPC message, discovery, summary tool revalidation callback and
  result/SSE emission: checked again, including after awaited work.
- Generic MCP `GET /onboarding`, `GET /start_here`, `GET /surface-prompt`:
  renewable summary-resource tokens rejected, not admitted to broader context.
- A new bearer fingerprint requires a fresh MCP initialize/session; an old
  `Mcp-Session-Id` is not transferable to a renewed token.

## Operational Limits

The existing DCR registry remains process-local. Restart removes registration
and invalidates renewal until a fresh registration/authorization. This is
accepted only for the dormant source foundation, not a usable managed-profile
attachment or high-availability OAuth service. Migration 021, separately
reviewed runtime capability/registration, explicit grants/consent and rollout
are prerequisites to activation. No production attachment/renewal test or
provider exactly-once guarantee is established by offline tests.

The unit suite exercises real routing/issuer/authority orchestration with
synthetic snapshots. The PostgreSQL suite requires an explicitly disposable
`ELLA_TEST_POSTGRES_DSN`, private schemas, real independent connections,
concurrent rotation, replay, lock-wait expiry and owner deletion. Never point
it at production. CI keeps all prior summary contracts and adds this separate
required no-skips refresh block.
