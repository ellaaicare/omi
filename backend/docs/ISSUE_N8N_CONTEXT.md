# GitHub Issue / PRD: n8n First-Escalation Context Pull

**Title:** feat: Only pull context on first escalation to avoid duplicates

**Labels:** enhancement, n8n

---

## Problem

Currently, when the scanner escalates to the main agent, it pulls the full conversation context from the backend every time. This causes:

1. **Duplicate context in Letta**: Letta already stores conversation history internally
2. **Confusion on subsequent turns**: Same messages appear twice
3. **Wasted API calls**: Fetching data Letta already has

### Example Flow (Current - Broken)

```
Turn 1:
  iOS → "hey ella" → Scanner → Escalate to Main
  Main receives: { context: ["hey ella"], message: "hey ella" }
  Main responds: "yes I hear you"

Turn 2:
  iOS → "what time is it" → Scanner → Escalate to Main
  Main receives: { context: ["hey ella", "yes I hear you", "what time"], message: "what time" }
  BUT Letta already has: ["hey ella", "yes I hear you"]
  Result: Duplicates!
```

## Solution

Only pull context from backend on **FIRST** escalation per conversation. Letta maintains state for subsequent turns.

### Proposed Flow (Fixed)

```
Turn 1 (first escalation):
  Scanner detects wake word
  Scanner: is_first_escalation = true
  Scanner calls GET /v1/conversations/in-progress
  Scanner sends to Main: { context: [...], first_escalation: true }
  Main stores context in Letta

Turn 2+ (subsequent):
  Scanner detects follow-up
  Scanner: is_first_escalation = false
  Scanner does NOT call context endpoint
  Scanner sends to Main: { message: "...", first_escalation: false }
  Main uses Letta's internal history
```

## Implementation Options

### Option A: n8n Tracks State (Recommended)

n8n workflow caches `conversation_id` after first escalation:

```javascript
// In scanner workflow
const conversationId = input.conversation_id;
const escalationCache = $workflow.getVariable('escalation_cache') || {};

if (!escalationCache[conversationId]) {
  // First escalation - pull context
  const context = await fetch('GET /v1/conversations/in-progress');
  escalationCache[conversationId] = true;
  $workflow.setVariable('escalation_cache', escalationCache);

  return { context, first_escalation: true };
} else {
  // Subsequent - skip context
  return { first_escalation: false };
}
```

### Option B: Backend Tracks Escalation

Add flag to in-progress endpoint:

```
GET /v1/conversations/in-progress?mark_escalated=true

Response:
{
  "id": "conv-123",
  "first_escalation": true,  // false on subsequent calls
  "transcript_segments": [...]
}
```

Backend stores `escalated_at` timestamp in conversation.

### Option C: Check Letta Message Count

Before pulling context, check if Letta already has messages for this conversation:

```javascript
const lettaMessages = await getLettaConversationHistory(conversationId);
if (lettaMessages.length > 0) {
  // Letta has context, skip pull
  return { first_escalation: false };
}
```

## API Details

### Current: GET /v1/conversations/in-progress

Returns:
```json
{
  "id": "conversation-uuid",
  "transcript_segments": [
    {"text": "hey ella", "role": "user", ...},
    {"text": "yes I hear", "role": "assistant", ...}
  ],
  "status": "in_progress"
}
```

The `id` field IS the conversation_id - use this for caching.

## Acceptance Criteria

- [ ] Context only fetched on first escalation
- [ ] Letta's internal history used for subsequent turns
- [ ] No duplicate messages in conversation
- [ ] Escalation state resets when conversation ends

## Notes

- Conversation ends when: WebSocket closes, 120s timeout, or explicit finalization
- n8n should clear escalation cache when receiving "conversation_ended" signal
- Backend will add `role` field to segments (see partner issue: ISSUE_ASSISTANT_TAGGING.md)
