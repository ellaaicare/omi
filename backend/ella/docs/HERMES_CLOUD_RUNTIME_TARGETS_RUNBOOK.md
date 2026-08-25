# Hermes Cloud Runtime Targets — Flags-Off Deploy Runbook

Issues: `ellaaicare/ella-ai#1124`, `ellaaicare/ella-ai#1126`,
`ellaaicare/ella-ai#1123`, `ellaaicare/ella-ai#1182`.

## Source contract

- Retained/Mini routing is preserved by a NULL Cloud target. Do not create a
  default Cloud target for an account, profile, role, or mode.
- A Cloud route is usable only when `ella_runtime_targets` has a ready row for
  the authenticated account/profile, target mode, endpoint ref, credential ref,
  and exact `ella_runtime_bindings.id`.
- OMI transcript enrichment is admitted through the published
  `hermes-cloud-transcript` target. There is no separate enrichment target and
  no default-mode fallback.
- Photon is admitted only through the published `hermes-cloud-photon` target.
  Preflight and inbound handling must resolve that exact mode; Photon cannot
  borrow chat, voice, transcript, Guardian, retained Mini, or Plato authority.
- Immediately before every protected Hermes Cloud POST, the sender must
  re-resolve that exact target and revalidate current entitlement
  status/revision/expiry, kill switches, binding health, consent lineage,
  profile class, and endpoint/credential identity. Any drift after admission
  produces zero provider sends. This invariant covers runtime turns, Guardian,
  Observer, summary/correction, recovery, and reinterpretation paths.
- Ready Cloud bindings must be profile-owned (`account_user_id = profile_user_id
  = user_id`), healthy, active, and in `internal_canary` or `active`.
- Cloud memory is built-in Hermes profile-scoped memory. Do not provision,
  disclose, or require Honcho Cloud on the Cloud route.
- Legacy retained Plato/Honcho paths remain separate and must not be modified by
  the Cloud target migration.
- Invitation-owned self-hosted Hermes is usable only with a
  `honcho-isolation-v2` HMAC attestation. OMI creates a 360-second nonce
  challenge binding the exact Firebase UID, internal account owner,
  invitation-target family, binding id, and provisioning job id. The
  provisioner must read the profile-local `<profile>/honcho.json` and sign the
  complete fixed-schema response: challenge fields, profile/config-path hash,
  workspace-root hash, Honcho workspace and both peer ids, gateway port and
  target hash, credential-reference hash, agent id, and service label. OMI
  verifies freshness, exact context, readback, and integrity before staging,
  again inside the activation transaction, and again inside each invitation
  resolution transaction. Database strings or mutually agreeing unsigned
  response objects are not runtime authority.
- The provisioner and OMI must receive the same separately scoped
  `ELLA_HERMES_PROVISION_ATTESTATION_KEY` (minimum 32 bytes). Source discovers
  secret-like environment credentials, dynamic Hermes keys, and configured
  authority-secret references on every verification. Accessor-observed values
  remain in the process separation set so reload cannot hide a cached runtime
  credential. Equality is rejected before provider work, activation, and
  resolution.
  Missing, whitespace, padded, short, equal, stale, partial, or incorrectly
  signed evidence fails closed. This source contract requires provisioner
  support and independently staged credentials before rollout.
- `ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS` remains bounded to 30-300 seconds.
  It plus `ELLA_HERMES_PROVISION_ATTESTATION_VERIFICATION_GRACE_SECONDS`
  (default 30 seconds) forms one monotonic deadline over response streaming,
  bounded decode, proof verification, staging, activation, and publication.
  That total plus the clock-skew margin must be strictly less than the
  360-second wall-clock proof lifetime; invalid combinations fail before
  external or binding work.
- Every request carries a stable content-free `Idempotency-Key` derived from the
  exact UID, invitation target, provisioning job, and deterministic binding.
  The provisioner must atomically reconcile that key to one existing or newly
  created profile/runtime, return a fresh response for each new nonce, and
  reject any attempt to reuse the key across UID/target/job/binding context.
  OMI treats post-provider malformed, stale, context-invalid, or MAC-invalid
  evidence as retryable without staging or publication, then retries the same
  job/binding with a fresh challenge. This external idempotency behavior is a
  mandatory deployment gate; do not enable rollout until it is independently
  exercised against the actual provisioner.
- Existing invitation bindings without the persisted v2 attestation fail
  closed. Re-provision them through the reviewed provider path; do not
  synthesize or backfill the proof from database values.
- `ai-data-processors-v7` and `ai-data-processors-v8` are immutable historical
  consent. Cloud target traffic requires `ai-data-processors-v10`,
  `managed-cloud-internal-pilot-v4`, and the current processor/scope hashes.
- Managed-cloud consent mutation and invitation redemption share the same
  PostgreSQL per-UID advisory lock and authority row. Redemption locks the exact
  granted epoch through entitlement insertion. Decline/revocation advances the
  epoch and atomically revokes the entitlement/targets and quarantines the Cloud
  binding before Firestore is updated. Thus redemption either commits before
  revocation and is immediately quarantined, or revocation wins and redemption
  performs no durable invite/capacity/redemption mutation. PostgreSQL or
  Firestore partial failure always leaves Cloud authority unusable.

## Flags that must stay off for source deploy

```text
ELLA_RUNTIME_BINDINGS_ENABLED=false
ELLA_RUNTIME_BINDINGS_ENABLED_UIDS=
ELLA_HERMES_PROVISIONING_ENABLED=false
ELLA_HERMES_PROVISIONING_ENABLED_UIDS=
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED=false
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS=
ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true
ELLA_HERMES_CLOUD_SYNTHETIC_UIDS=
ELLA_INVITE_REDEMPTION_ENABLED=false
ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED=false
ELLA_INVITE_APP_REVIEW_ENABLED=false
```

Deploying source with these flags off must not claim pool capacity, invite a
real user, migrate live data, contact vendors, or route authenticated traffic to
Cloud.

## Migration order

Apply only after source approval and database backup:

```bash
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/008_create_voice_canary_controls.sql
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/009_create_hermes_cloud_runtime_pool.sql
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/010_add_cloud_profile_class.sql
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/011_create_invitation_redemption.sql
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/012_create_account_profile_runtime_targets.sql
psql -X --set=ON_ERROR_STOP=1 "$ELLA_POSTGRES_DSN" --file=backend/migrations/013_create_managed_cloud_consent_authority.sql
```

Migrations 011, 012, and 013 contain their own `BEGIN`/`COMMIT` boundary. The
`ON_ERROR_STOP` setting is mandatory: any statement failure must exit nonzero
and roll the whole migration back.

Verify:

```sql
SELECT to_regclass('ella_runtime_targets');
SELECT indexname FROM pg_indexes WHERE indexname LIKE 'ella_runtime_targets_%' ORDER BY indexname;
SELECT conname FROM pg_constraint WHERE conname IN (
  'ella_runtime_bindings_cloud_target_shape_check',
  'ella_runtime_bindings_cloud_pool_shape_check'
);
```

## Synthetic promotion sequence

1. Keep global flags off.
2. Add only the pilot Firebase UID to
   `ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS` and
   `ELLA_HERMES_CLOUD_SYNTHETIC_UIDS`.
3. Ensure `users.profile_class = 'synthetic'` for that disposable profile only.
4. Require invite redemption, active invite entitlement, exact v8 consent,
   capacity, and voice canary admission before `claim_cloud_pool_binding`.
5. Register only Honcho-free Hermes Cloud pool candidates.
6. Promote from `shadow` only through revision-checked CAS. Promotion creates
   ready targets for chat, voice, transcript, Guardian, and Photon modes.

Rollback is source/config only while flags remain off. If a claim reaches
`claiming`, rollback quarantines the candidate and records a content-free receipt;
it does not attempt Honcho cleanup because Cloud memory is not Honcho-backed.

## One-account staged-attestation exception

Use this only for the isolated synthetic canary when Nous direct
`/health/detailed` and `/v1/*` preflight are unavailable. Normal candidates
continue through live direct preflight.

The protected receipt reference must be an immediate child of
`/var/lib/ella/hermes-cloud-attestations`, whose directory is root:root `0700`.
The receipt must be a regular, single-link, root-owned `0400` or `0600` JSON
file. It is content-free: no URL, credential, token, payload, or secret.
Its exact `ella-hermes-cloud-staged-attestation-v1` fields pin:

- attestation/issue/expiry metadata and
  `stage=pool_registration_and_claim_finalization`;
- exact synthetic UID plus canonical account/profile UUIDs;
- runtime instance, template, voice policy, model, tools, capabilities, prompt
  pack/model policy, and context window;
- policy commit, approved-manifest SHA-256, and the three prompt artifact
  SHA-256 values.

Activation uses the existing root-run CLI with a protected candidate JSON. The
candidate adds only `synthetic_uid`, `account_id`, `profile_id`, and
`staged_attestation_ref`; secret values remain behind the existing `env:` refs:

```bash
python backend/scripts/hermes_cloud_pool_admin.py register \
  --candidate /var/lib/ella/hermes-cloud-attestations/canary-candidate.json
```

`ELLA_HERMES_CLOUD_STAGED_ATTESTATION_ENABLED=true` and
`ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true` are required. Every ordinary global
rollout flag listed above remains false, and each of these selectors must equal
the one UID: `ELLA_RUNTIME_BINDINGS_ENABLED_UIDS`,
`ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS`,
`ELLA_HERMES_CLOUD_SYNTHETIC_UIDS`, and
`ELLA_AI_CONSENT_ENFORCEMENT_UIDS`.

Registration emits only binding, attestation digest/id, and exact rollback IDs.
Before claim finalization, OMI reopens the same protected receipt and rechecks
all pins. Roll back an unclaimed registration without SQL:

```bash
python backend/scripts/hermes_cloud_pool_admin.py cleanup \
  --binding-id <exact-binding-uuid> \
  --runtime-instance-id <exact-runtime-instance-id>
```

After a claim starts, the existing fail-closed quarantine/rollback receipt owns
recovery. General multi-user attestations, replay registries, and leases remain
deferred to `ellaaicare/ella-ai#1157`.
