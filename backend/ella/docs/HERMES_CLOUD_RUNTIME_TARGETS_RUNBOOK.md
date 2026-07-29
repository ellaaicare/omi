# Hermes Cloud Runtime Targets — Flags-Off Deploy Runbook

Issues: `ellaaicare/ella-ai#1124`, `ellaaicare/ella-ai#1126`,
`ellaaicare/ella-ai#1123`.

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
- `ai-data-processors-v7` is immutable historical consent. Cloud target traffic
  requires `ai-data-processors-v8`, `managed-cloud-internal-pilot-v2`, and the
  current processor/scope hashes.
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
