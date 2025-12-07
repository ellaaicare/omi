# PRD: Voice Mode Call State Integration

**Document Version**: 1.0
**Created**: December 7, 2025
**Author**: Backend Team
**GitHub Issue**: [#28](https://github.com/ellaaicare/omi/issues/28)
**Related**: n8n Call State PRD (`/Users/greg/repos/ella-ai/docs/prd/BACKEND_CALL_STATE_INTEGRATION.md`)

---

## Executive Summary

Integrate call state notifications into the voice mode v2 pipeline. When a voice session starts or ends, notify n8n so the scanner can skip processing during active calls and call logs can be maintained.

---

## Current System State

### Voice Mode v2 Flow

```
iOS App → WebSocket /v2/voice → manual_pipeline.py
                                      │
                                      ├── initialize()
                                      │   └── Fetch config from n8n
                                      │
                                      ├── run()
                                      │   └── Audio loop (VAD → STT → LLM → TTS)
                                      │
                                      └── cleanup()
                                          ├── Save to Firestore
                                          └── Call memory/summary agents (fire-and-forget)
```

### n8n Call State Endpoint (Already Deployed)

```
POST https://n8n.ella-ai-care.com/webhook/call-state
Content-Type: application/json

{
  "action": "start|end|status|heartbeat",
  "uid": "<firebase_uid>",
  "params": { ... }
}
```

---

## Implementation Design

### 1. Config Changes (`config.py`)

Add call state webhook path to N8NConfig:

```python
@dataclass
class N8NConfig:
    """n8n webhook endpoints configuration."""

    base_url: str = "https://n8n.ella-ai-care.com"
    voice_config_path: str = "/webhook/voice-config"
    memory_agent_path: str = "/webhook/memory-agent"
    summary_agent_path: str = "/webhook/summary-agent"
    call_state_path: str = "/webhook/call-state"  # NEW
    timeout_seconds: float = 10.0

    @property
    def call_state_url(self) -> str:  # NEW
        return f"{self.base_url}{self.call_state_path}"
```

### 2. N8N Client Changes (`n8n_client.py`)

Add two new methods:

```python
async def notify_call_start(
    self,
    uid: str,
    session_id: str,
    call_type: str = "voice_mode",
    initiated_by: str = "user",
) -> None:
    """
    Notify n8n that a voice call has started.

    Fire-and-forget - logs errors but doesn't block.
    """
    payload = {
        "action": "start",
        "uid": uid,
        "params": {
            "call_type": call_type,
            "call_sid": session_id,
            "initiated_by": initiated_by,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                self.config.call_state_url,
                json=payload,
            )
            if response.status_code == 200:
                print(f"📞 Call state START: {session_id[:8]}")
            else:
                print(f"⚠️ Call state START failed: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Call state START error: {e}")


async def notify_call_end(
    self,
    uid: str,
    session_id: str,
    ended_by: str = "user",
    status: str = "completed",
) -> None:
    """
    Notify n8n that a voice call has ended.

    Fire-and-forget - logs errors but doesn't block.
    """
    payload = {
        "action": "end",
        "uid": uid,
        "params": {
            "call_sid": session_id,
            "ended_by": ended_by,
            "status": status,
        }
    }

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.post(
                self.config.call_state_url,
                json=payload,
            )
            if response.status_code == 200:
                print(f"📞 Call state END: {session_id[:8]}")
            else:
                print(f"⚠️ Call state END failed: {response.status_code}")
    except Exception as e:
        print(f"⚠️ Call state END error: {e}")
```

### 3. Pipeline Integration (`manual_pipeline.py`)

**In `initialize()` - after fetching config:**
```python
async def initialize(self):
    # ... existing code ...

    # Notify n8n that call started
    asyncio.create_task(
        self.n8n_client.notify_call_start(
            uid=self.uid,
            session_id=self.session_id,
            call_type="voice_mode",
            initiated_by="user",
        )
    )
```

**In `cleanup()` - before calling agents:**
```python
async def cleanup(self):
    self.is_running = False

    # Determine how call ended
    ended_by = "timeout" if self._cleanup_reason == "timeout" else "user"

    # Notify n8n that call ended
    await self.n8n_client.notify_call_end(
        uid=self.uid,
        session_id=self.session_id,
        ended_by=ended_by,
        status="completed",
    )

    # ... rest of existing cleanup code ...
```

---

## ended_by Logic

| Scenario | `ended_by` Value |
|----------|------------------|
| WebSocket disconnect (user closed app) | `"user"` |
| Session timeout (60s no activity) | `"timeout"` |
| Error/exception | `"system"` |
| Normal completion | `"user"` |

Default: `"user"` (most common case for voice mode)

---

## Error Handling

- All call state notifications are **fire-and-forget**
- Errors are logged but do NOT block the voice pipeline
- Short timeout (5s) to avoid blocking
- n8n has auto-expiry cron for stale states (worst case: 5 min stale)

---

## Testing

### Manual Test

```bash
# Start a voice session via iOS app
# Check VPS logs for:
journalctl -u omi-backend -f | grep "Call state"

# Expected output:
# 📞 Call state START: abc12345
# ... voice session ...
# 📞 Call state END: abc12345
```

### Verify n8n Received

```bash
# Check call state
curl -X POST "https://n8n.ella-ai-care.com/webhook/call-state" \
  -H "Content-Type: application/json" \
  -d '{"action":"status","uid":"<test_uid>"}'
```

---

## Acceptance Criteria

- [x] Issue created: #28
- [ ] `call_state_path` added to N8NConfig
- [ ] `notify_call_start()` method added to N8NClient
- [ ] `notify_call_end()` method added to N8NClient
- [ ] Call state notifications integrated in manual_pipeline.py
- [ ] Errors logged but don't block voice pipeline
- [ ] Tested on VPS with real voice session

---

## Future Extensions

1. **Heartbeat**: For calls > 5 minutes, send periodic heartbeat
2. **Twilio Integration**: Same interface, different `call_type` and `call_sid`
3. **Inbound Calls**: Use `initiated_by: "agent"` for agent-initiated calls

---

## Files Modified

| File | Change |
|------|--------|
| `integrations/pipecat/pipeline/config.py` | Add `call_state_path` and property |
| `integrations/pipecat/services/n8n_client.py` | Add `notify_call_start()` and `notify_call_end()` |
| `integrations/pipecat/pipeline/manual_pipeline.py` | Call notification methods |
| `docs/PRD_VOICE_CALL_STATE.md` | This document |
