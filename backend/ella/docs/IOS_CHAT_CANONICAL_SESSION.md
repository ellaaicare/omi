# iOS Chat Canonical Session Contract

iOS Chat must share the same memory lane as iMessage, voice postbacks, Guardian,
and MCP reads. The backend enforces this by routing iOS Chat through
`/v1/ella/chat/stream` and writing both sides of each turn to the canonical
ledger.

## Runtime Flow

1. iOS sends chat text to `/v1/ella/chat/stream`.
2. Backend fetches `/v1/ella/timeline` context before answering.
3. Backend writes the user turn to `/v1/ella/events` storage internally as:
   - `channel=ios_chat`
   - `provider=omi-ios-chat`
   - `role=user`
   - `scan_policy=immediate`
4. Backend sends the prompt to Hermes with the canonical timeline injected as
   the freshest shared context.
5. Backend writes the assistant turn as:
   - `channel=ios_chat`
   - `provider=omi-ios-chat`
   - `role=assistant`
   - `scan_policy=none`
6. iOS history reads `/v1/ella/chat/history`, which is canonical timeline first.

## Session Strategy

Default Hermes session key:

```text
ella:omi:{uid}:canonical
```

This is intentionally not an `ios-chat:daily-*` lane. Source-specific lanes can
still exist, but they must inject canonical timeline context and write canonical
turns. If lane isolation is needed for migration testing, set:

```text
ELLA_CHAT_HERMES_SESSION_SCOPE=lane
ELLA_CHAT_HERMES_SESSION_EPOCH=<explicit epoch>
```

The production default should remain `canonical` to avoid split-brain behavior
between iOS Chat and iMessage.

## Stable Event IDs

iOS passes `client_message_id` and `client_sent_at`. Backend creates stable ids:

```text
source_identity = ios_chat:{uid}:{client_message_id}
event_id        = ios_chat:{uid}:{client_message_id}:{role}
```

If old clients omit `client_message_id`, backend derives a server id from
`uid`, message text, and timestamp as migration fallback.

## Smoke Test

Use a test user/contact only:

1. Send iOS Chat: "Remember the demo banana is on the blue shelf."
2. Confirm `/v1/ella/timeline?uid=<uid>&channels=ios_chat` contains user and
   assistant turns in chronological order.
3. Ask iMessage: "Where is the demo banana?"
4. Confirm answer uses the iOS Chat fact.
5. Send iMessage: "The demo mug is in the garage."
6. Ask iOS Chat: "Where is the demo mug?"
7. Confirm answer uses canonical iMessage context.

Do not rely on Hermes private session history as proof of success; canonical
timeline is the source of truth.
