# Ella Invite Redemption Control Plane

Issue: `ellaaicare/ella-ai#1126`. Product and security contract:
`ellaaicare/ella-ai#1115`.

## Authority and boundaries

- Firebase bearer authentication is the only source of the UID.
- `POST /v1/invite/redeem` accepts exactly `{"code":"..."}`.
- The code is normalized and stored only as a domain-separated HMAC-SHA-256.
- UID audit references and source-address references use separate HMAC domains.
- Issuance creates one or more HMAC-only server-owned account/profile target
  rows. Redemption derives both identities from the authenticated first-party
  profile binding and must match one target before entitlement or capacity
  changes.
- Redemption requires an exact current `ai-data-processors-v8` grant, presence
  in both exact Hermes Cloud provisioning and synthetic UID allowlists, and a
  persisted `users.profile_class = 'synthetic'`. Global rollout flags do not
  satisfy this pilot-only gate.
- The transaction locks the invitation and capacity reservation, creates the
  `invited` voice entitlement, records redemption/audit receipts, consumes
  ordinary capacity, and marks an ordinary invitation redeemed.
- Provisioning is not performed by this endpoint. After commit, the client uses
  the existing authenticated `/v1/ella/onboarding/ensure` receipt. That receipt
  remains the single owner of the #1124 warm-pool claim.
- Voice allow/deny and quota frames remain server-authoritative under #1113.

## Feature gates

All gates default off:

```text
ELLA_INVITE_REDEMPTION_ENABLED=false
ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED=false
ELLA_INVITE_APP_REVIEW_ENABLED=false
ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS=
ELLA_HERMES_CLOUD_SYNTHETIC_UIDS=
```

`ELLA_INVITE_HMAC_PEPPER` is required when the endpoint is enabled. Store it in
the production secret manager. Do not put it in a shell history, deployment
receipt, migration, issue, workflow JSON, or log.

`ELLA_INVITE_TRUSTED_PROXY_IPS` is an optional comma-separated list of exact
local reverse-proxy addresses. Forwarded source headers are ignored unless the
direct peer is on this list.

Phase 1 keeps ordinary self-service off. Enabling the global route alone does
not permit ordinary or App Review redemption. The exact UID must be present in
both cloud allowlists, have a current v8 consent receipt bound to the same
account/profile, and have a synthetic database profile classification.

## Migration

Apply only after the integrated #1124 migration lineage is approved:

```bash
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/008_create_voice_canary_controls.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/009_create_hermes_cloud_runtime_pool.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/010_add_cloud_profile_class.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/011_create_invitation_redemption.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/012_create_account_profile_runtime_targets.sql
```

The migration is forward-only and idempotent. It adds:

- capacity reservations;
- HMAC-only invitations;
- HMAC-only account/profile issuance targets and exact consent-contract pins;
- invitation attribution on `voice_entitlements`;
- append-only successful redemption and audit receipts;
- bounded HMAC-only rate events;
- deduplicated anomaly alerts.
- account/profile-owned runtime targets that keep retained Mini routing NULL and
  require exact ready Hermes Cloud binding/endpoint/credential/mode ownership.

It does not seed codes, grants, or production users.

## Verification

Run secret-free structural checks:

```sql
SELECT to_regclass('ella_invitation_capacity_reservations');
SELECT to_regclass('ella_invitations');
SELECT to_regclass('ella_invitation_targets');
SELECT to_regclass('ella_invitation_redemptions');
SELECT to_regclass('ella_invitation_audit_receipts');
SELECT to_regclass('ella_invitation_rate_limit_events');

SELECT column_name
FROM information_schema.columns
WHERE table_name = 'voice_entitlements'
  AND column_name IN (
    'invitation_id',
    'entitlement_policy_revision',
    'cohort',
    'exclude_from_product_analytics'
  )
ORDER BY column_name;
```

Verify flags remain off before deploying source. An unauthenticated entitlement
read and redemption must return 401. With the global route disabled, a valid
synthetic code must remain unconsumed and must not create an entitlement.

## Promotion order

1. Independently review and merge the backend source.
2. Back up schema metadata and apply/verify migrations 008, 009, 010, 011, and 012 in
   release order.
3. Deploy the exact reviewed OMI backend and voice proxy revisions with all
   invitation flags off.
4. Run authenticated synthetic/non-family reads and disabled-redemption checks.
5. Keep global cloud and ordinary self-service flags off. Enable only the exact
   synthetic test UID in both cloud allowlists and issue its HMAC-bound target.
6. Verify same-UID retry, two-UID exclusion, typed failures, rate limiting,
   canonical entitlement, #1124 ensure idempotency, and authoritative quota
   frames.
7. Roll all invitation flags back off before any incident investigation.

## Rollback and disable

Immediate rollback is configuration-only:

```text
ELLA_INVITE_REDEMPTION_ENABLED=false
ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED=false
ELLA_INVITE_APP_REVIEW_ENABLED=false
```

Do not drop migration 011 tables during rollback. They hold audit and
idempotency evidence. Existing entitlements remain under the #1113 operator
grant/suspend/revoke controls. The voice global kill switch remains the
authoritative emergency stop.

No production invitation, entitlement, vendor workspace, or provisioning claim
may be created until the AI consent gate in #1123 and the independent release
review are green.
