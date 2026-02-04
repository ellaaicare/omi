# Upstream Merge Status — 2026-02-03

**Branch**: `feature/ella-v2-fresh`
**Upstream Base**: `upstream/main` @ `7b4df1119` (2026-02-03)
**Commit**: `7bd9d6f76`

---

## What Changed

Rebased Ella fork onto upstream/main, absorbing **673 upstream commits**.
Ella-specific code re-applied as a clean 6-file overlay (+123 lines).

### Upstream Files Patched

| File | Lines Added | Purpose |
|------|------------|---------|
| `main.py` | +9 | `try: from ella import register_ella_extensions` at bottom |
| `utils/llm/clients.py` | +84 | Context vars + ELLA_LLM_BASE_URL priority chain |
| `routers/chat.py` | +4 | `set_ella_context(uid, task='chat')` |
| `routers/transcribe.py` | +15 | `set_ella_context` + `send_to_scanner` hook |
| `utils/llm/conversation_processing.py` | +14 | Ella summary adapter hook |
| `utils/conversations/process_conversation.py` | +3 | Pass `uid=` to structure function |

### Ella-Only Directories (not in upstream)

- `ella/` — Extension registration, routers, adapters, compat layer
- `utils/ella/` — Scanner, summary, memory, chat modules

### Pruned (not brought back)

- `routers/testing.py`, `routers/tts.py`, `routers/ai.py`
- `routers/analytics.py`, `routers/voice_v2.py`
- Redundant docs under `backend/docs/`

---

## Production Status

**URL**: `https://api.ella-ai-care.com`
**Service**: `omi-backend.service` — active (running)

### Ella Extensions Loaded

```
🏥 ELLA AI CARE - Backend Extensions Loading
📦 Registered adapter: summary
📦 Registered adapter: memory
📦 Registered adapter: scanner
📦 Registered 3 adapters
🌐 /v1/ella/* - Callback endpoints
🌐 /v1/voice/* - Voice session endpoints
🏥 n8n Base URL: https://n8n.ella-ai-care.com
🏥 Grok Proxy:   wss://voice.ella-ai-care.com/ws
```

---

## MCP Endpoints — Test Guide

### Authentication

All MCP endpoints use Bearer token auth:

```
Authorization: Bearer omi_mcp_<key>
```

Keys are created in the Omi app (Settings > Developer > MCP), or via the
`database.mcp_api_key.create_mcp_key(uid, name)` function on the VPS.

### REST Endpoints

#### List Memories

```bash
curl -s 'https://api.ella-ai-care.com/v1/mcp/memories?limit=10' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY"
```

Returns: `[{"id": "...", "content": "...", "category": "..."}]`

#### Create Memory

```bash
curl -s -X POST 'https://api.ella-ai-care.com/v1/mcp/memories' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"content": "User prefers morning meetings", "category": "work"}'
```

#### Delete Memory

```bash
curl -s -X DELETE 'https://api.ella-ai-care.com/v1/mcp/memories/MEMORY_ID' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY"
```

#### Update Memory

```bash
curl -s -X PATCH 'https://api.ella-ai-care.com/v1/mcp/memories/MEMORY_ID' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"value": "Updated content"}'
```

#### List Conversations

```bash
curl -s 'https://api.ella-ai-care.com/v1/mcp/conversations?limit=5' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY"
```

Returns: `[{"id": "...", "structured": {"title": "...", "overview": "..."}}]`

Optional filters: `start_date`, `end_date`, `categories` (comma-separated)

#### Get Full Conversation (with transcript)

```bash
curl -s 'https://api.ella-ai-care.com/v1/mcp/conversations/CONV_ID' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY"
```

Returns conversation + `transcript_segments` array with speaker names.

### MCP SSE Protocol (for Claude Desktop / MCP clients)

The full MCP 2025-03-26 Streamable HTTP protocol is at `/v1/mcp/sse`.

#### Server Info (no auth)

```bash
curl -s https://api.ella-ai-care.com/v1/mcp/sse/info
```

#### Initialize Session

```bash
curl -si -X POST 'https://api.ella-ai-care.com/v1/mcp/sse' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{}}'
```

Response headers include `Mcp-Session-Id` for subsequent requests.

#### List Tools

```bash
curl -s -X POST 'https://api.ella-ai-care.com/v1/mcp/sse' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/list","params":{}}'
```

#### Call a Tool

```bash
curl -s -X POST 'https://api.ella-ai-care.com/v1/mcp/sse' \
  -H "Authorization: Bearer omi_mcp_YOUR_KEY" \
  -H "Content-Type: application/json" \
  -H "Mcp-Session-Id: SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"get_memories","arguments":{"limit":5}}}'
```

### Available MCP Tools

| Tool | Description |
|------|-------------|
| `get_memories` | List memories (filter by category, paginate) |
| `create_memory` | Add a new memory |
| `delete_memory` | Delete by ID |
| `edit_memory` | Update content by ID |
| `get_conversations` | List conversations (filter by date/category) |
| `get_conversation_by_id` | Full conversation with transcript segments |

### Memory Categories

`interesting`, `system`, `manual`, `core`, `hobbies`, `lifestyle`,
`interests`, `habits`, `work`, `skills`, `learnings`, `other`, `auto`

### Conversation Categories

`personal`, `education`, `health`, `finance`, `legal`, `philosophy`,
`spiritual`, `science`, `entrepreneurship`, `parenting`, `romantic`,
`travel`, `inspiration`, `technology`, `business`, `social`, `work`,
`sports`, `politics`, `literature`, `history`, `architecture`, `music`,
`weather`, `news`, `entertainment`, `psychology`, `real`, `design`,
`family`, `economics`, `environment`, `other`

---

## Claude Desktop / MCP Client Config

Add to `~/.claude/claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "omi": {
      "url": "https://api.ella-ai-care.com/v1/mcp/sse",
      "headers": {
        "Authorization": "Bearer omi_mcp_YOUR_KEY"
      }
    }
  }
}
```

---

## Verification Checklist

- [x] Service running on VPS
- [x] Ella extensions loaded (3 adapters, 2 routers)
- [x] MCP REST endpoints responding (memories, conversations)
- [x] MCP SSE protocol working (initialize, tools/list, tools/call)
- [x] Auth via `omi_mcp_*` keys working
- [x] n8n webhook back online
- [ ] Test with Claude Desktop MCP client
- [ ] Test WebSocket transcription flow
- [ ] Verify Ella summary adapter fires on new conversations
