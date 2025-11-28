# GitHub Issue: Assistant Message Tagging

**Title:** feat: Add assistant message tagging to transcript segments

**Labels:** enhancement, backend

---

## Problem

When Ella responds to users, the response is not stored in the conversation transcript. This causes issues on subsequent turns where the LLM sees:

```
User: hey ella, let me know if you can hear this
User: yes I hear you, what's up?  ← This is actually Ella's response!
User: what time is it today
```

The LLM gets confused thinking the user is repeating Ella's words.

## Solution

1. Add `role` field to TranscriptSegment model: `"user" | "assistant"`
2. Modify `/v1/ella/notification` endpoint to store assistant message in transcript
3. Use `speaker="ELLA_ASSISTANT"` as identifier
4. Ensure diarization doesn't overwrite assistant tags

## Technical Details

- TranscriptSegment already has `is_user` field for speaker identification
- Audio diarization (Deepgram/Soniox) sets `is_user` based on speech profile
- New `role` field distinguishes: user speech, other speakers, assistant responses

### Fields

```python
class TranscriptSegment(BaseModel):
    # Existing
    is_user: bool = False  # True if device owner (via speech profile)
    speaker: str = "SPEAKER_00"  # Speaker ID from diarization

    # New
    role: str = "user"  # "user" | "assistant" | "other"
```

### Endpoint Changes

`POST /v1/ella/notification`:
- After generating TTS, store message in in-progress conversation
- Set `role="assistant"`, `speaker="ELLA_ASSISTANT"`

## Acceptance Criteria

- [ ] Assistant messages stored in transcript with `role="assistant"`
- [ ] `GET /v1/conversations/in-progress` returns role-tagged segments
- [ ] Audio diarization preserves existing assistant tags (doesn't overwrite)
- [ ] Context formatting shows: `User: ...` and `Assistant: ...`

## Related

- Partner issue: n8n first-escalation context (see ISSUE_N8N_CONTEXT.md)
