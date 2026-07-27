# Ella Invite Redemption Control Plane

Issue: `ellaaicare/ella-ai#1126`. Product and security contract:
`ellaaicare/ella-ai#1115`.

## Authority and boundaries

- Firebase bearer authentication is the only source of the UID.
- `POST /v1/invite/redeem` accepts exactly `{"code":"..."}`.
- The code is normalized and stored only as a domain-separated HMAC-SHA-256.
- UID audit references and source-address references use separate HMAC domains.
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
```

`ELLA_INVITE_HMAC_PEPPER` is required when the endpoint is enabled. Store it in
the production secret manager. Do not put it in a shell history, deployment
receipt, migration, issue, workflow JSON, or log.

`ELLA_INVITE_TRUSTED_PROXY_IPS` is an optional comma-separated list of exact
local reverse-proxy addresses. Forwarded source headers are ignored unless the
direct peer is on this list.

Phase 1 keeps ordinary self-service off. Enabling the global route alone does
not permit ordinary or App Review redemption.

## Migration

Apply only after migration 008 and the #1124 migration 009 release order is
approved:

```bash
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/011_create_invitation_redemption.sql
```

The migration is forward-only and idempotent. It adds:

- capacity reservations;
- HMAC-only invitations;
- invitation attribution on `voice_entitlements`;
- append-only successful redemption and audit receipts;
- bounded HMAC-only rate events;
- deduplicated anomaly alerts.

It does not seed codes, grants, or production users.

## Verification

Run secret-free structural checks:

```sql
SELECT to_regclass('ella_invitation_capacity_reservations');
SELECT to_regclass('ella_invitations');
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
2. Back up schema metadata and apply/verify migrations 008, 009, and 010 in
   release order.
3. Deploy the exact reviewed OMI backend and voice proxy revisions with all
   invitation flags off.
4. Run authenticated synthetic/non-family reads and disabled-redemption checks.
5. Enable the global route and only the synthetic/App Review cohort needed for
   the approved test. Keep ordinary self-service off.
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
