# Hermes Cloud synthetic product-fit canary

This harness evaluates one disposable synthetic OMI profile without changing
production routing. It uses the existing stock broker client, authenticated
voice-memory scope, and authenticated enrichment route. It does not call Plato,
Honcho, Mini, or a provider directly.

## Safety boundary

- Run as root with one protected JSON config at an absolute path under
  `/var/lib/ella` or `/etc/ella`.
- The config file must be root-owned mode `0400` or `0600`; its immediate
  directory must be root-owned mode `0700`.
- Put only `env:ELLA_...` secret references in the config. Load the referenced
  values into the harness process without placing them in argv or shell history.
- Use only a UID beginning with `synthetic-` or `staging-synthetic-`, exact
  account/profile/binding UUIDs, and two distinct explicit session keys.
- The backend URL is HTTPS/443 only. The broker URL retains the production
  client's HTTPS/443 rule and permits only its reviewed synthetic loopback pin,
  `http://127.0.0.1:18097`.
- The harness never prints prompts, responses, tokens, memory content,
  transcripts, or callback bodies. Reports contain hashes, equality facts,
  counts, status names, and bounded latencies only.

## Protected config contract

```json
{
  "schema_version": "ella-hermes-product-fit-canary-v1",
  "run_id": "synthetic-fit-YYYYMMDD",
  "selectors": {
    "uid": "staging-synthetic-REPLACE",
    "account_id": "00000000-0000-4000-8000-000000000000",
    "profile_id": "00000000-0000-4000-8000-000000000000",
    "binding_id": "00000000-0000-4000-8000-000000000000",
    "consent_epoch": "00000000-0000-4000-8000-000000000000",
    "expected_model": "gpt-5.6-terra",
    "chat_channel": "ios_chat",
    "primary_session_key": "ella:canary:REPLACE:primary",
    "isolated_session_key": "ella:canary:REPLACE:isolated"
  },
  "backend": {
    "base_url": "https://REPLACE",
    "auth_token_ref": "env:ELLA_CANARY_FIREBASE_ID_TOKEN",
    "enrichment_token_ref": "env:ELLA_HERMES_CLOUD_ENRICHMENT_TOKEN"
  },
  "broker": {
    "base_url": "http://127.0.0.1:18097",
    "allowed_host": "127.0.0.1",
    "service_token_ref": "env:ELLA_HERMES_BROKER_SERVICE_TOKEN",
    "poll_interval_seconds": 0.5,
    "poll_timeout_seconds": 45,
    "deadline_seconds": 90
  },
  "voice_memory": {
    "conversation_id": "synthetic-memory-REPLACE",
    "active_summary_version_id": "synthetic-summary-REPLACE",
    "pack_sha256": "REPLACE_WITH_64_LOWERCASE_HEX"
  },
  "enrichment": {
    "conversation_id": "synthetic-enrichment-REPLACE",
    "active_summary_version_id": "synthetic-enrichment-summary-REPLACE",
    "transcript_sha256": "REPLACE_WITH_64_LOWERCASE_HEX"
  },
  "max_latency_ms": 120000
}
```

`voice_memory.pack_sha256` is the SHA-256 of the content-free `session_scope`
projection returned only after the server-owned full memory pack resolves. The
exact conversation and active summary version pins prove the intended pack was
selected without returning its content. Compute the projection hash inside an
approved protected process; do not print or persist the memory pack. The
enrichment transcript and active summary version must already exist under the
exact disposable synthetic profile. The harness does not seed content or fabricate database rows. It never
calls the voice context route, so this check cannot invoke Honcho.

## Run

From `backend/`:

```bash
python scripts/hermes_product_fit_canary.py run \
  --config /var/lib/ella/hermes-product-fit/canary.json
```

Exit status is `0` only when every verdict is `PASS`, `1` when any verdict is
`FAIL`, and `2` when no verdict fails but at least one capability is
`NOT TESTED`. Each scenario emits one content-free stage table followed by this
matrix:

- chat transport
- full-response fidelity
- API/SSE-like consumption
- same-session continuity
- cross-session isolation
- profile memory pack
- enrichment
- replay safety

The callback contract fixture is `ella.hermes.callback.v1` and requires the
`outcome` field. Broker completion remains owner/request/correlation/lane pinned
with `stock_best_effort_v1`, `terminal_proof=false`, one generation, and one
terminal writeback.

Scenario E obtains its `client_interaction_id` from the production enrichment
identity boundary. That boundary binds UID, conversation ID, transcript SHA-256,
the current enrichment policy version, and the SHA-256 of the current policy
instructions. The canary `run_id` is not part of this canonical identity, so an
exact retry reaches the production duplicate-safe path. Successful enrichment
receipts may include canonical user and assistant event IDs. Chat session
key/ID fields remain forbidden and cause the contamination check to fail.

## Cleanup

Cleanup is deliberately two-phase so the harness cannot mutate n8n or deployment
configuration. First, the authorized controller restores all canary workflows,
global flags, and exact selectors to off/empty and writes a root-owned protected
receipt:

```json
{
  "schema_version": "ella-hermes-canary-off-receipt-v1",
  "uid_sha256": "REPLACE_WITH_SHA256_OF_EXACT_SYNTHETIC_UID",
  "flags_off": true,
  "selectors_empty": true,
  "workflows_off": true,
  "content_free": true
}
```

Then invoke the existing exact-UID voice-data deletion and authenticated
exact-account deletion paths:

```bash
python scripts/hermes_product_fit_canary.py cleanup \
  --config /var/lib/ella/hermes-product-fit/canary.json \
  --off-receipt /var/lib/ella/hermes-product-fit/off.receipt.json \
  --confirm-uid staging-synthetic-REPLACE
```

Cleanup refuses UID drift, a missing or non-protected receipt, or any receipt
that does not attest all flags, selectors, and workflows are off. The bearer
token stays in-process; output is a content-free receipt containing only the UID
hash, removed-row count, and cleanup facts. Invitation revocation/classification cleanup and
unclaimed pool cleanup remain in their existing reviewed operator CLIs when
those artifacts still exist.
