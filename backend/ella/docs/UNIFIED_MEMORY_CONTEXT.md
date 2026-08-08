# Unified Memory Context Contract

This is the developer contract for Ella context reads and source ingestion.
New devices, apps, agents, and MCP tools should use the canonical ledger first.

## Current Status

The single canonical read call is:

```http
GET /v1/ella/timeline?uid=<uid>&limit=<n>&since=<iso>&channels=<csv>
```

The single canonical write call for source adapters is:

```http
POST /v1/ella/events
```

This endpoint is hosted by the OMI backend on the Ella API host:

```text
https://api.ella-ai-care.com/v1/ella/timeline
https://api.ella-ai-care.com/v1/ella/events
```

The Plato/Grok MCP bridge uses this read path through
`ELLA_PLATO_TIMELINE_URL`, which defaults to
`https://api.ella-ai-care.com/v1/ella/timeline`.

## Authority

User-facing calls require a Firebase bearer and may read or write only the
exact token subject. Internal adapters use the distinct
`X-Ella-Event-Ledger-Key` header backed by `ELLA_EVENT_LEDGER_TOKEN`; every
event and session completion in a service batch is still checked for a
non-empty, matching `uid` and `canonical_identity`. Missing configuration,
mixed user authority, and caller-selected cross-user identifiers fail closed.

## What "Unified" Means

All consumer surfaces should hydrate recent context from `GET /v1/ella/timeline`
instead of reading OMI conversations, memories, iMessage rows, Hermes files, or
OpenClaw workspace files directly.

This applies to:

- iOS chat UI
- iOS voice mode
- OMI necklace/voice context
- iMessage/Hermes adapters
- Guardian/scanner policy paths
- Telegram or other source adapters
- External MCP connectors such as Grok
- Any future OpenAI/Gemini/custom assistant connector

If a caller needs "what just happened?" context, the default answer is one call
to `/v1/ella/timeline` for the user's canonical identity. Source-specific
fallbacks are temporary migration guards, not the contract for new work.

## Read Contract

Example:

```bash
curl 'https://api.ella-ai-care.com/v1/ella/timeline?uid=5aGC5YE9BnhcSoTxxtT4ar6ILQy2&channels=omi,ios_chat,imessage&limit=50'
```

Parameters:

- `uid`: required user id. Today this is the canonical profile uid used by the
  backend. Future multi-role access should resolve account/user/caregiver
  authorization before calling the timeline.
- `limit`: optional, default 100, max 500. The endpoint returns the most recent
  window sorted chronologically ascending inside that window.
- `since`: optional ISO timestamp lower bound.
- `channels`: optional comma-separated filter, for example
  `omi,ios_chat,ios_voice,imessage,telegram,memory,observer_memory,companion_observation,grok_conversation`.
  Default hot-context callers include OMI, app chat, voice, iMessage,
  Telegram, Guardian, memory, observer memory, and external companion
  observation channels so MCP/Grok writes can appear in normal startup context
  without a deep search.

Response shape:

```json
{
  "ok": true,
  "uid": "user_uid",
  "events": [
    {
      "uid": "user_uid",
      "canonical_identity": "user_uid",
      "event_id": "omi:<conversation_id>:summary",
      "source_identity": "omi:<conversation_id>",
      "session_id": "<conversation_id>",
      "channel": "omi",
      "provider": "omi-backend",
      "role": "user",
      "text": "Human-readable event text",
      "started_at": "2026-05-07T18:56:59.312831+00:00",
      "ended_at": null,
      "privacy_scope": "user_private",
      "scan_policy": "none",
      "source_ref": {},
      "metadata": {},
      "raw_event": {}
    }
  ]
}
```

## Write Contract

Source adapters should submit lossless events to `/v1/ella/events` before any
downstream summary, memory, scanner, or agent-specific processing.

Required fields:

- `uid`
- `canonical_identity`
- `event_id`
- `channel`
- `provider`
- `role`
- `text`
- `started_at`

Recommended fields:

- `session_id`
- `ended_at`
- `privacy_scope`
- `scan_policy`
- `source_ref.source_identity`
- `metadata`

Idempotency:

- The dedupe key is `(event_id, source_identity)`.
- Adapters must provide stable source ids, not random retry ids.
- Normal raw events are immutable on duplicate.
- Derived OMI enriched summary rows may update the existing canonical row so the
  app-visible summary can be corrected without creating duplicate timeline
  entries.

## Source Adapter Rules

Adapters should map source-specific identity into stable canonical ids.

Examples:

| Source | Channel | Provider | Stable id guidance |
| --- | --- | --- | --- |
| OMI enriched conversation | `omi` | `omi-backend` | `event_id=omi:<conversation_id>:summary`, `source_identity=omi:<conversation_id>` |
| iOS chat | `ios_chat` | `ella-ios` | stable message id/session id from app/backend |
| iOS voice | `ios_voice` | `ella-ios` or voice provider | voice session id plus turn id |
| iMessage | `imessage` | `hermes-imessage` | chat.db `rowid`/`guid` |
| Telegram | `telegram` | `telegram-bridge` | `update_id`, `message_id`, `chat_id`, `from_id` |
| Guardian/scanner output | `guardian` | `ella-scanner` | trace id plus policy/delivery item id |

Use `scan_policy=immediate` only for fresh human turns that should be eligible
for scanner/guardian review. Use `scan_policy=none` for assistant turns,
derived summaries, backfills, and bot/system artifacts unless a policy owner
explicitly chooses otherwise.

## Current MCP Read Surface

The Plato/Hermes MCP bridge currently exposes read-only tools:

- `plato_recent_context`: reads recent context from canonical timeline.
- `plato_search_memory`: searches the recent canonical context window.
- `plato_latest_omi`: returns the latest OMI event from the canonical context.
- `plato_consult`: injects fresh canonical context into a constrained Hermes
  consult call.
- `plato_get_scanner_rules`: reads scanner rule files through the provision API.

The MCP bridge still has a Firestore fallback for OMI while old deployments and
older rows are being migrated. New development should treat that fallback as a
temporary guard only.

## MCP Write Surface Direction

External MCP clients should not directly edit Hermes/OpenClaw markdown files or
OMI conversation records. Those are implementation stores with their own
post-processing and enrichment rules. Direct edits from Grok or another
external agent would bypass audit, identity, scanner policy, and OMI writeback
flows.

Preferred design:

1. MCP write tools create append-only canonical events or explicit proposals.
2. Hermes/Admin policy decides whether a proposal becomes memory, chat,
   scanner config, or an OMI summary correction.
3. Dedicated backend workers perform the actual downstream writeback through
   the correct owner API.

Recommended future MCP tools:

| Tool | Purpose | Backend target | Safety posture |
| --- | --- | --- | --- |
| `plato_create_context_note` | Add a user/caregiver supplied fact or note | `POST /v1/ella/events` with `channel=external_mcp` | append-only, auditable |
| `plato_send_agent_message` | Ask Hermes to respond in a session | Hermes chat gateway | no caregiver delivery by default |
| `plato_create_memory_proposal` | Suggest durable memory | canonical proposal event, then memory worker | reviewable before promotion |
| `plato_propose_omi_summary_update` | Suggest a correction to an OMI conversation summary | proposal event, then OMI enrichment/writeback worker | do not patch OMI directly from MCP |
| `plato_create_scanner_rule_proposal` | Suggest scanner/rule changes | proposal event or provision API draft | no direct markdown write from external MCP |
| `plato_create_task` | Create a follow-up task for user/caregiver/team | canonical task/proposal event | role/scoped authorization required |

OMI conversation updates are intentionally tricky. The app-visible summary is
owned by the OMI backend enrichment/writeback flow. A future MCP correction tool
should therefore write a correction proposal with:

- target `conversation_id`
- target summary version, if known
- proposed correction text
- evidence from canonical events
- requesting principal and role
- audit trace id

A backend enrichment worker should then decide whether to call the OMI summary
writeback endpoint and create a new summary version. The MCP tool should not
overwrite OMI Firestore directly.

## Role And Access Model

The current Plato MCP bridge is scoped to one configured Plato profile. That is
acceptable for controlled testing, but not enough for family/caregiver product
use.

Future auth needs first-class roles:

- user viewing their own timeline
- caregiver viewing an authorized family member's timeline
- user who is also a caregiver for another person
- admin/test operator using dry-run tools

The MCP tool layer should resolve the actor and target profile before every
read or write. A single connector can expose multiple authorized profiles, but
tool arguments must make the target explicit once more than one profile is
available.

## Development Rule

For new context features:

1. Write raw/source events to `/v1/ella/events`.
2. Read context from `/v1/ella/timeline`.
3. Add source-specific fallback only as a temporary migration guard.
4. Keep downstream stores behind owner APIs or workers.
5. Add tests that prove the user-visible path reads canonical data, not a
   private source-specific fallback.
