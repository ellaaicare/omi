# Ella Post-Merge Patch Registry

**Purpose**: Track Ella-required patches to upstream-managed OMI files so they can be verified and re-applied after syncing from Basehardware upstream.

**Last updated**: 2026-04-11

Core rule: keep policy and custom behavior in `backend/ella/` whenever possible. If an upstream-managed file must change, make it a small hook that delegates to `backend/ella/`, and document the exact file, behavior, and verification command here.

---

## Patch 1: Ella Extension Registration

**Status**: Existing

**Upstream file**: `backend/main.py`

**Ella-owned code**: `backend/ella/__init__.py`

**Purpose**: Register Ella routers, adapters, and compatibility patches.

**Post-merge check**:

```bash
grep -n "register_ella_extensions" backend/main.py
cd backend && python -c "from ella import register_ella_extensions; print('Ella loads OK')"
```

---

## Patch 2: Ella LLM Context Routing

**Status**: Existing

**Upstream files**:

- `backend/utils/llm/clients.py`
- `backend/routers/chat.py`
- `backend/routers/transcribe.py`
- `backend/utils/conversations/process_conversation.py`
- `backend/utils/llm/conversation_processing.py`

**Ella-owned code**:

- `backend/ella/config.py`
- `backend/utils/ella/`

**Purpose**: Route selected chat, summary, memory, and scanner work through Ella-aware adapters/proxy context while preserving upstream fallback behavior.

**Post-merge check**:

```bash
grep -R "set_ella_context\|send_to_scanner\|uid=" \
  backend/routers/chat.py \
  backend/routers/transcribe.py \
  backend/utils/conversations/process_conversation.py \
  backend/utils/llm/conversation_processing.py
```

---

## Patch 3: Reprocessing Payload Data Route

**Status**: Existing from #579

**Upstream files**:

- `backend/ella/routers/callbacks.py` for the Ella conversation data endpoint.
- `packages/openclaw/scripts/provision-api.py` in `ella-ai`, not this OMI repo, for `_build_reprocess_payload()`.

**Purpose**: Ensure reprocessing sends full transcript/segment/timestamp data to n8n instead of a stub payload.

**Post-merge check**:

```bash
grep -R "conversation/.*/data\|conversation_id}/data" backend/ella/routers/callbacks.py
```

---

## Patch 4: Max-Duration Conversation Split

**Status**: Implemented for ellaaicare/ella-ai#609

**Problem**: `backend/routers/transcribe.py` only splits conversations after silence. Long car rides, podcasts, TV, or continuous media can produce multi-hour single conversations, which harms summary quality and increases LLM cost/context risk.

**Preferred implementation**:

Keep the configuration and decision helper in Ella-owned code, with one small hook in upstream-managed transcription lifecycle code.

**Ella-owned files**:

- `backend/ella/config.py`
  - Environment-backed settings:
    - `ELLA_CONVERSATION_MAX_DURATION_ENABLED`
    - `ELLA_CONVERSATION_MAX_DURATION_SECONDS` (default: 1800, 30 minutes)
- `backend/ella/services/conversation_lifecycle.py`
  - Pure helper: `should_split_for_max_duration(conversation, now, user_preferences)`.
  - Returns a structured decision with `reason`, `elapsed_seconds`, and `limit_seconds`.

**User preference API**:

- `GET /v1/users/conversation-lifecycle-preferences`
- `PATCH /v1/users/conversation-lifecycle-preferences`

Nullable user fields use server defaults:

- `conversation_max_duration_enabled`
- `conversation_max_duration_seconds`

Sending either field as JSON `null` clears that user override.

**Upstream-managed files requiring hooks**:

- `backend/routers/transcribe.py`
- `backend/routers/users.py`
- `backend/database/users.py`

**Why a core hook is required**:

The active websocket stores `current_conversation_id` in local `_stream_handler` state. A separate Ella-only background scanner could mark an old conversation as processing, but the live websocket may continue appending segments to the old local ID until the core lifecycle loop notices. The clean split must happen inside `conversation_lifecycle_manager()` where it can call `_process_conversation(current_conversation_id)` and immediately `_create_new_in_progress_conversation()` without closing the websocket.

**Hook location**:

Inside `conversation_lifecycle_manager()` after loading the current conversation and before/alongside the existing silence timeout check.

**Expected behavior**:

- Keep `conversation_timeout` as the silence timeout.
- Add an Ella-configured hard max duration.
- Process when either condition is true:
  - `now - finished_at >= conversation_creation_timeout`
  - `now - started_at >= ELLA_CONVERSATION_MAX_DURATION_SECONDS`
- After processing, create a new in-progress conversation and keep the websocket alive.

**Post-merge check**:

```bash
grep -n "conversation_lifecycle\|ELLA_CONVERSATION_MAX_DURATION\|max duration" backend/routers/transcribe.py backend/ella/config.py backend/ella/services/conversation_lifecycle.py
```

**Test expectations**:

- A simulated stream with continuous segment updates rotates after the configured max duration.
- The silence timeout still rotates after inactivity.
- The websocket remains connected after max-duration rotation.
- Subsequent scanner chunks use the new conversation ID.
