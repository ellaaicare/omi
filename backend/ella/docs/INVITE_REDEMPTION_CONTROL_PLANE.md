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
- Redemption requires an exact current `ai-data-processors-v10` grant, presence
  in the runtime-binding, Hermes Cloud provisioning, Hermes Cloud synthetic,
  and AI-consent exact UID allowlists, and a persisted
  `users.profile_class = 'synthetic'`. Global rollout flags must remain false.
- The transaction locks the invitation and capacity reservation, creates the
  `invited` voice entitlement, records redemption/audit receipts, consumes
  ordinary capacity, and marks an ordinary invitation redeemed.
- Provisioning is not performed by this endpoint. After commit, the client uses
  the existing authenticated `/v1/ella/onboarding/ensure` receipt. That receipt
  remains the single owner of the `ellaaicare/ella-ai#1124` warm-pool claim.
- Voice allow/deny and quota frames remain server-authoritative under
  `ellaaicare/ella-ai#1113`.

### Invitation-only self-hosted launch

The self-hosted launch lane is separate from the synthetic Hermes Cloud pilot:

- `/v1/invite/redeem` still authenticates with Firebase and derives the email
  only from a nonempty `email_verified=true` claim. The request body contains
  only the invitation code.
- An ordinary invitation may bind a domain-separated HMAC of that verified
  email. An open App Review invitation still requires a verified email and is
  bounded by the migration-011 reviewer quota.
- Redemption atomically binds the Firebase UID/email to one PostgreSQL user,
  consumes invitation capacity, creates the pending entitlement, and reserves
  exact `hermes-chat` and `hermes-voice` runtime targets beneath the same
  consumed invitation target. The pair is unique by invitation target and
  mode. A losing concurrent or mismatched
  redemption creates none of those protected records.
- The entitlement and target remain unusable until the unconditional
  `/v1/ella/onboarding/ensure` current-consent dependency publishes the exact
  current consent authority epoch and clears `invitation_consent_pending` in
  the same PostgreSQL transaction.
- New provisioning admission requires the exact invitation entitlement,
  account/profile owner, current consent epoch, v8 lineage, exact `hermes`
  provider, `gpt-5.6-sol` model, chat/voice mode allowlists, disabled fallback,
  and the exact reserved/ready mode target. Activation locks and publishes the
  chat/voice pair in one transaction. Invitation mode remains authoritative
  during chat, `/v1/voice/session`, voice-proxy acceptance, and every proxy
  re-resolution; the voice token pins a digest of the exact account, profile,
  binding, target, entitlement revision, and consent epoch. There is no post-sign-in UID
  allowlist and no fallback to Hermes Cloud, OpenClaw, shared, default, or a
  retained runtime.
- An owner-locked transaction revalidates that complete authority chain after
  the external provisioner returns and immediately before activation. Revoke,
  decline, or consent-authority drift atomically revokes the invitation target
  and entitlement, disables bindings, removes sessions, and blocks future
  claims. Re-granting consent cannot reactivate a revoked invitation target.
- Ordinary capacity is a transactionally advisory-locked ceiling of five
  active non-review pilot slots. App Review has a separate, no-expiry capacity
  reservation: at most 20 redemptions with two reserved setup slots.

The root-only `pilot_invite_admin.py` operator writes the plaintext code once
to an approved root-owned `0400` file. A scoped email is accepted only through
`--email-input-file`, which must name an immediate regular, single-link,
root-owned `0400` or `0600` child of the same root-owned `0700` approved
directory. The CLI opens both directory and input with no-follow semantics,
reads the email in-process, immediately derives the domain-separated HMAC, and
never accepts or emits the address through argv, receipts, logs, errors, or
command examples. `issue`, `show`, `rotate`, and `revoke` print only
content-free receipts. Rotation copies the email HMAC and policy pins inside
one transaction, revokes/releases the previous invitation, and is idempotent
when the protected recovery file proves the same operation. Ordinary issuance
requires an absolute timezone-qualified `--expires-at`; the absolute expiry is
part of the protected recovery identity, so a lost-receipt retry using the same
output path deterministically recovers the existing invitation. App Review
issuance forbids an expiry. Consent remains mandatory for every redemption,
including App Review.

Prepare the optional scoped input inside a trusted root session without putting
the value in shell history or a child process argument:

```bash
install -d -o root -g root -m 0700 /root/ella-invites
install -o root -g root -m 0600 /dev/null /root/ella-invites/scope.input
read -rsp 'Verified invitation address: ' ELLA_INVITE_SCOPED_ADDRESS
printf '%s\n' "$ELLA_INVITE_SCOPED_ADDRESS" > /root/ella-invites/scope.input
unset ELLA_INVITE_SCOPED_ADDRESS
chmod 0400 /root/ella-invites/scope.input

python backend/scripts/pilot_invite_admin.py issue \
  --kind ordinary \
  --email-input-file /root/ella-invites/scope.input \
  --expires-at 2026-12-01T00:00:00Z \
  --expected-environment production \
  --approved-code-output-root /root/ella-invites \
  --code-output-file /root/ella-invites/pilot.code
```

## Feature gates

All gates default off:

```text
ELLA_INVITE_REDEMPTION_ENABLED=false
ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED=false
ELLA_INVITE_APP_REVIEW_ENABLED=false
ELLA_SELF_HOSTED_PROVISIONING_ENABLED=false
ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true
ELLA_RUNTIME_BINDINGS_ENABLED=false
ELLA_RUNTIME_BINDINGS_ENABLED_UIDS=
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED=false
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS=
ELLA_HERMES_CLOUD_SYNTHETIC_UIDS=
ELLA_AI_CONSENT_ENFORCEMENT_ENABLED=false
ELLA_AI_CONSENT_ENFORCEMENT_UIDS=
```

`ELLA_INVITE_HMAC_PEPPER` is required when the endpoint is enabled. Store it in
the production secret manager. Do not put it in a shell history, deployment
receipt, migration, issue, workflow JSON, or log.

`ELLA_INVITE_TRUSTED_PROXY_IPS` is an optional comma-separated list of exact
local reverse-proxy addresses. Forwarded source headers are ignored unless the
direct peer is on this list.

Phase 1 keeps ordinary self-service off. Enabling the redemption route alone
does not permit ordinary or App Review redemption. The exact UID must be
present in all four pilot allowlists, have a current v8 consent receipt bound to
the same account/profile, and have a synthetic database profile
classification.

## Migration

Apply only after the integrated `ellaaicare/ella-ai#1124` migration lineage is
approved:

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
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/013_create_managed_cloud_consent_authority.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/014_add_synthetic_invitation_operator_audit.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/015_add_invitation_allowed_email_hash.sql
psql "$ELLA_POSTGRES_DSN" \
  -f backend/migrations/017_add_voice_entitlement_consent_revision.sql
```

Migration `015` is reserved for this invitation lane. Open
`ellaaicare/omi#360` has renumbered its Today migration to `016`; it still must
rebase after this lane before merge. This PR does not modify or merge #360.

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
- a per-UID managed-cloud consent authority epoch that serializes consent
  mutation through invitation entitlement publication and revocation quarantine.
- content-free audit event types for the root-only synthetic operator lifecycle.
- verified-email HMAC scope, explicit pending-consent entitlement/redemption
  state, and the exact reserved/ready self-hosted chat/voice runtime-target pair.
- a phased redemption mapping for migration-011 history: unambiguous rows are
  mapped to exact users, valid multi-redemption App Review history is retained
  as `legacy_unmapped`, and the post-015 consent-shape constraint is validated
  only after that classification.

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
2. Back up schema metadata and apply/verify migrations 008 through 015 in
   release order, after the migration-number dependency above is resolved.
3. Deploy the exact reviewed OMI backend and voice proxy revisions with all
   invitation flags off.
4. Run authenticated synthetic/non-family reads and disabled-redemption checks.
5. Keep global cloud and ordinary self-service flags off. Add only the exact
   synthetic test UID to the four pilot allowlists and issue its HMAC-bound
   target through the protected operator ceremony.
6. Verify same-UID retry, two-UID exclusion, typed failures, rate limiting,
   canonical entitlement, `ellaaicare/ella-ai#1124` ensure idempotency, and authoritative quota
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
idempotency evidence. Existing entitlements remain under the
`ellaaicare/ella-ai#1113` operator
grant/suspend/revoke controls. The voice global kill switch remains the
authoritative emergency stop.

No production invitation, entitlement, vendor workspace, or provisioning claim
may be created until the AI consent gate in `ellaaicare/ella-ai#1123` and the independent release
review are green.

The protected one-profile synthetic issuance lifecycle is documented in
`SYNTHETIC_INVITE_OPERATOR.md`. It keeps ordinary/App Review admission off and
does not send codes or seed broker/runtime tables.
