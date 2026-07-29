# Synthetic Invitation Operator

This is the root-only, one-profile ceremony for the disposable Hermes Cloud
prototype. It issues the existing eight-character invitation format consumed by
`POST /v1/invite/redeem`; it does not email or text a code, create a user, seed
broker/runtime tables, or enable a rollout.

## Preconditions

Apply migration `014_add_synthetic_invitation_operator_audit.sql` after
migrations 008-013. It only extends the existing invitation-audit event
constraint.

The exact Firebase UID, account UID, and profile UID must be identical and use
the reserved `synthetic-` or `staging-synthetic-` prefix. The PostgreSQL user
must already be `ACTIVE` and explicitly classified
`profile_class = 'synthetic'`. Issuance refuses a `real` profile, an existing
entitlement, provisioning job, runtime binding/target, managed-cloud authority,
any prior operator invitation history, or the protected Plato identities.
The disposable authenticated account must already hold the exact current v8 AI
consent grant and receipt in Firestore. Issuance reads that authority and stores
only its HMAC-backed SQL lineage; it does not create or bypass consent.

The exact UID must appear in all four lists:

```text
ELLA_RUNTIME_BINDINGS_ENABLED_UIDS
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS
ELLA_HERMES_CLOUD_SYNTHETIC_UIDS
ELLA_AI_CONSENT_ENFORCEMENT_UIDS
```

Keep these global rollout flags false:

```text
ELLA_RUNTIME_BINDINGS_ENABLED=false
ELLA_HERMES_PROVISIONING_ENABLED=false
ELLA_HERMES_CLOUD_PROVISIONING_ENABLED=false
ELLA_AI_CONSENT_ENFORCEMENT_ENABLED=false
ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED=false
ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED=false
ELLA_ISOLATED_VOICE_ROUTING_ENABLED=false
ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED=false
ELLA_INVITE_APP_REVIEW_ENABLED=false
```

The synthetic and redemption gates are the only required non-false gates:

```text
ELLA_HERMES_CLOUD_SYNTHETIC_ONLY=true
ELLA_INVITE_REDEMPTION_ENABLED=true
ELLA_INVITE_OPERATOR_ENVIRONMENT=<exact-environment-name>
GOOGLE_CLOUD_PROJECT=<exact-firestore-project>
```

`ELLA_INVITE_HMAC_PEPPER` must be a server-side secret of at least 32 bytes.
Never place its value in a command, issue, receipt, log, or code-output file.
Run the command with approved Application Default Credentials for the exact
`GOOGLE_CLOUD_PROJECT`. The CLI constructs the Firestore client directly in
memory; it does not serialize `SERVICE_ACCOUNT_JSON` or any credential.

## Protected code handoff

Create one dedicated directory before issuance:

```bash
install -d -o root -g root -m 0700 /var/lib/ella/invite-codes
```

The CLI accepts an immediate child of that directory only. It opens the
directory and file with no-follow semantics, creates the file exclusively, and
finishes with root ownership and mode `0400`. An existing secure file is read
internally only to recover an interrupted, already-committed issuance; it is
never printed.

Enter the approved root session with the server environment and ADC already
loaded. Do not preserve an untrusted caller environment or `PYTHONPATH`. From
`backend/`, use one fixed UTC expiry no more than 24 hours ahead:

```bash
python scripts/synthetic_invite_admin.py issue \
  --uid SYNTHETIC_UID \
  --account-uid SYNTHETIC_UID \
  --profile-uid SYNTHETIC_UID \
  --expected-environment EXPECTED_ENVIRONMENT \
  --expected-database EXPECTED_DATABASE \
  --expected-firestore-project EXPECTED_FIRESTORE_PROJECT \
  --operator OPERATOR_ID \
  --expires-at 2026-07-30T00:00:00Z \
  --approved-code-output-root /var/lib/ella/invite-codes \
  --code-output-file /var/lib/ella/invite-codes/ONE_TIME_RECEIPT.code
```

Stdout contains only a content-free invitation receipt UUID and protected path
reference. If the process outcome is uncertain, rerun the exact same command and
path. A matching database invitation converges to the same receipt; a stale
file, different code, drifted binding, collision, or terminal invitation state
fails closed. Use `show` to obtain its state, expiry, and version.

The approved Infra/owner ceremony may read the protected file interactively
from the root session. Do not use a command that copies it into shell history,
logs, issue comments, chat, or a process argument. The executable order is:
create the disposable authenticated account/profile, record its current v8
consent grant, issue this invitation, redeem it through authenticated
`POST /v1/invite/redeem` (which revalidates the same v8 authority before any SQL
mutation), then continue with `/v1/ella/onboarding/ensure` and provisioning.

## Inspect, revoke, and clean up

Keep the exact synthetic-only gate, all four UID allowlists, and all global
flags in the states above until cleanup is complete. `show`, `revoke`, and
`cleanup` recheck them before touching PostgreSQL. `show` never reads or reveals
the code:

```bash
python scripts/synthetic_invite_admin.py show \
  --uid SYNTHETIC_UID \
  --account-uid SYNTHETIC_UID \
  --profile-uid SYNTHETIC_UID \
  --expected-environment EXPECTED_ENVIRONMENT \
  --expected-database EXPECTED_DATABASE \
  --operator OPERATOR_ID \
  --receipt-id RECEIPT_UUID
```

Before redemption, revoke with the exact version returned by `show`:

```bash
python scripts/synthetic_invite_admin.py revoke \
  --uid SYNTHETIC_UID \
  --account-uid SYNTHETIC_UID \
  --profile-uid SYNTHETIC_UID \
  --expected-environment EXPECTED_ENVIRONMENT \
  --expected-database EXPECTED_DATABASE \
  --operator OPERATOR_ID \
  --receipt-id RECEIPT_UUID \
  --expected-version VERSION
```

After revocation, or after the invitation has expired, cleanup releases its
reservation and changes only that still-unredeemed disposable PostgreSQL
profile from `synthetic` back to `real`. It refuses redeemed entitlements,
runtime/provisioning artifacts, binding drift, or a stale version:

```bash
python scripts/synthetic_invite_admin.py cleanup \
  --uid SYNTHETIC_UID \
  --account-uid SYNTHETIC_UID \
  --profile-uid SYNTHETIC_UID \
  --expected-environment EXPECTED_ENVIRONMENT \
  --expected-database EXPECTED_DATABASE \
  --operator OPERATOR_ID \
  --receipt-id RECEIPT_UUID \
  --expected-version VERSION
```

The invitation, target HMACs, and content-free audit receipts remain as
idempotency evidence. Broker tables and the `realcryptoplato` / `plato_eval`
route are outside this ceremony.
