# Ella Chat Flow Architecture

**Last Updated**: 2026-02-07
**Status**: Production — Grok direct (debug_level=2) or OpenClaw via LLM proxy (ELLA_LLM_BASE_URL)

## Current Production Config (VPS)

```
ELLA_LLM_BASE_URL=http://localhost:8100/v1   ← Routes graph chat through LLM proxy
ELLA_LLM_MODEL=ella-enhanced                 ← Routes to OpenClaw agent
ELLA_DEBUG_LEVEL=2                           ← Ella debug endpoint uses Grok direct
XAI_API_KEY=xai-xxx                          ← Fallback for Ella debug endpoint
```

**Graph chat** (`/v2/messages`, what the app calls): Goes through LLM proxy → OpenClaw
**Ella debug chat** (`/v1/ella/chat/stream`): Goes direct to Grok (debug_level=2)

---

## Overview

The Ella chat system routes messages from the Flutter app through the OMI backend to an LLM. The architecture supports multiple backends (Grok, Letta, OpenClaw) via an OpenAI-compatible LLM proxy, keeping the app and OMI backend code unchanged.

---

## Data Flow Diagram

```
┌─────────────┐    ┌──────────────────┐    ┌─────────────────────┐    ┌──────────────┐
│  Flutter App │───►│  OMI Backend     │───►│  LLM Proxy          │───►│  OpenClaw    │
│  (iOS/And)   │    │  (VPS :8000)     │    │  (VPS :8100)        │    │  (dev :8090) │
│              │◄───│                  │◄───│                     │◄───│              │
└─────────────┘    └──────────────────┘    └─────────────────────┘    └──────────────┘
    SSE:               SSE:                    SSE:                      JSON/SSE
    data: <text>       OpenAI-compatible       OpenAI-compatible
    done: <b64json>    (LangChain ChatOpenAI)  (standard format)
    think: <text>
```

### Detailed Flow

1. **App → Backend** (`POST /v2/messages`)
   - App sends `{"text": "Hello", "file_ids": []}` with Bearer token
   - Backend extracts `uid` from Firebase token
   - Uses OMI's graph chat system (`execute_graph_chat_stream`)

2. **Backend → LLM** (via LangChain `ChatOpenAI`)
   - When `ELLA_LLM_BASE_URL` is set: routes to LLM proxy
   - When `XAI_API_KEY` is set: routes to xAI Grok directly
   - Fallback: routes to OpenAI
   - Ella patch injects `user=ella:{uid}:{task}` into every call

3. **LLM Proxy → Agent Backend** (Letta/OpenClaw)
   - Looks up user's agent via n8n webhook
   - Routes to appropriate agent backend
   - Converts Letta/OpenClaw SSE → OpenAI SSE format

4. **Backend → App** (OMI SSE protocol)
   - OMI backend converts LangChain streaming response to custom SSE:
     - `data: <text chunk>` — streaming content (newlines escaped as `__CRLF__`)
     - `done: <base64-encoded JSON>` — final message object
     - `think: <text>` — reasoning/thinking chunks
     - `message: <base64-encoded JSON>` — conversation context messages

---

## SSE Protocol Reference

### App-Side Parser (`parseMessageChunk` in `messages.dart`)

```dart
// Lines split on '\n\n', then parsed:
"data: Hello world"           → MessageChunkType.data, text="Hello world"
"think: Let me consider..."   → MessageChunkType.think
"done: eyJpZCI6Li4u"         → MessageChunkType.done (base64 JSON → ServerMessage)
"message: eyJpZCI6Li4u"      → MessageChunkType.message (base64 JSON → ServerMessage)
```

The `__CRLF__` escape is replaced with `\n` on the client side.

### OMI Backend SSE Output (`backend/routers/chat.py`)

```python
# Streaming chunks from LangChain callback:
yield f'{chunk.replace(chr(10), "__CRLF__")}\n\n'  # prefixed with "data: " by graph.py

# Final message:
encoded = base64.b64encode(response_message.model_dump_json().encode()).decode()
yield f"done: {encoded}\n\n"
```

### LLM Proxy SSE (OpenAI-compatible, `llm-proxy/main.py`)

```json
data: {"id":"chatcmpl-xxx","object":"chat.completion.chunk","choices":[{"delta":{"content":"Hello"}}]}
data: [DONE]
```

The OMI backend's LangChain `ChatOpenAI` client handles this format natively.

---

## Configuration

### VPS Backend `.env` (`/root/omi/backend/.env`)

```bash
# ====== LLM ROUTING ======
# Priority: ELLA_LLM_BASE_URL > XAI_API_KEY > OPENAI_API_KEY

# Option A: Route through Ella LLM Proxy (for Letta/OpenClaw)
# ELLA_LLM_BASE_URL=http://localhost:8100/v1
# ELLA_LLM_API_KEY=ella-internal
# ELLA_LLM_MODEL=ella-letta

# Option B: Direct xAI Grok (current production)
XAI_API_KEY=xai-xxx

# Option C: Direct OpenAI (upstream default)
# OPENAI_API_KEY=sk-xxx

# ====== ELLA CHAT ENDPOINT ======
# Debug levels for POST /v1/ella/chat/stream:
#   0 = production (uses same LLM as graph chat)
#   1 = ACK (hardcoded response, no LLM)
#   2 = Grok direct (xAI API)
#   3 = n8n webhook (full pipeline)
ELLA_DEBUG_LEVEL=2
ELLA_N8N_CHAT_WEBHOOK=https://n8n.ella-ai-care.com/webhook/ella-chat

# ====== LLM MODEL OVERRIDES ======
OMI_LLM_MINI=grok-4-1-fast-non-reasoning
OMI_LLM_MEDIUM=grok-4-1-fast-non-reasoning
OMI_LLM_LARGE=grok-4-1-fast-reasoning
```

### LLM Proxy `.env` (`/opt/ella/llm-proxy/.env`)

```bash
LETTA_URL=http://100.100.44.61:9284     # Letta on letta-iMac (Tailscale)
N8N_WEBHOOK_URL=https://n8n.ella-ai-care.com/webhook
DASHBOARD_URL=http://127.0.0.1:3002
PROXY_PORT=8100
AUTO_PROVISION_ENABLED=true
```

---

## Key Files

### OMI Repo (`/Users/greg/repos/omi/`)

| File | Purpose |
|------|---------|
| `app/lib/backend/http/api/messages.dart` | App-side: `sendMessageStreamServer()`, `parseMessageChunk()` |
| `app/lib/backend/http/shared.dart` | App-side: `makeStreamingApiCall()`, auth headers |
| `app/lib/providers/message_provider.dart` | App-side: `sendMessageStreamToServer()` orchestration |
| `backend/routers/chat.py` | OMI chat router: `/v2/messages` SSE streaming |
| `backend/utils/llm/clients.py` | LLM client config + Ella proxy patch |
| `backend/utils/retrieval/graph.py` | Graph chat execution + SSE callback |
| `backend/ella/routers/chat.py` | Ella debug chat: `/v1/ella/chat/stream` |
| `backend/ella/config.py` | `ELLA_CONFIG` dataclass (debug_level, n8n webhook URL) |

### Ella-AI Repo (`/Users/greg/repos/ella-ai/`)

| File | Purpose |
|------|---------|
| `services/llm-proxy/main.py` | LLM proxy: OpenAI-compat → Letta/OpenClaw SSE conversion |
| `services/llm-proxy/.env` | Proxy config (Letta URL, n8n URL, etc.) |
| `services/n8n-workflows/` | n8n workflow JSON exports |

### VPS (`ssh root@100.101.168.91`)

| Path | Purpose |
|------|---------|
| `/root/omi/backend/` | OMI backend deployment |
| `/root/omi/backend/.env` | Backend environment config |
| `/opt/ella/llm-proxy/` | LLM proxy deployment |
| `/opt/ella/llm-proxy/.env` | Proxy environment config |
| `/etc/caddy/Caddyfile` | Reverse proxy (api.ella-ai-care.com → :8000) |
| `/etc/systemd/system/omi-backend.service` | Backend systemd service |
| `/etc/systemd/system/ella-llm-proxy.service` | LLM proxy systemd service |

### OpenClaw Dev (`ssh plato@100.67.113.120`)

| Path | Purpose |
|------|---------|
| `/home/plato/ella-dev/services/omi-webhook-receiver/main.py` | Webhook receiver (port 8090) |
| Endpoints: `/webhook/health`, `/webhook/summary`, `/webhook/memory`, `/webhook/scanner` | |
| Auth: `X-Ella-Webhook-Secret` header | |

---

## Switching Between Backends

### Use Grok Direct (current production)
```bash
# /root/omi/backend/.env
# Comment out ELLA_LLM_BASE_URL
XAI_API_KEY=xai-xxx
sudo systemctl restart omi-backend
```

### Use LLM Proxy → Letta/OpenClaw
```bash
# /root/omi/backend/.env
ELLA_LLM_BASE_URL=http://localhost:8100/v1
ELLA_LLM_API_KEY=ella-internal
ELLA_LLM_MODEL=ella-letta
sudo systemctl restart omi-backend
```

### Use Ella Debug Endpoint Directly
```bash
# /root/omi/backend/.env
ELLA_DEBUG_LEVEL=3  # n8n pipeline
ELLA_N8N_CHAT_WEBHOOK=https://n8n.ella-ai-care.com/webhook/ella-chat
sudo systemctl restart omi-backend
```

---

## Ella Proxy Patch (How UID Flows Through)

Located in `backend/utils/llm/clients.py`:

```python
# Monkey-patches ChatOpenAI._generate to inject user context
def _patched_generate(self, messages, stop=None, run_manager=None, **kwargs):
    ctx = get_ella_context()
    if ctx.get('uid'):
        task = ctx.get('task', 'unknown')
        kwargs['user'] = f"ella:{ctx['uid']}:{task}"
    return _original_generate(self, messages, stop, run_manager, **kwargs)
```

The OMI chat router calls `set_ella_context(uid=uid, task='chat')` before processing, ensuring the UID is passed through to whatever LLM backend is configured.

The LLM proxy extracts the UID:
```python
# In llm-proxy/main.py
uid = x_user_id or request.user  # request.user = "ella:firebase_uid:chat"
if uid.startswith("ella:"):
    parts = uid.split(":")
    uid = parts[1]  # Extract firebase UID
```

---

## Troubleshooting

### Chat returns empty / no response
1. Check `ELLA_DEBUG_LEVEL` — set to 1 for ACK test
2. Check `XAI_API_KEY` is valid (for Grok direct mode)
3. Check LLM proxy health: `curl http://localhost:8100/health`

### 401 errors from app
1. Firebase token expired — app auto-refreshes
2. Backend not accepting token — check `ADMIN_KEY` in backend .env

### LLM proxy returns 404
1. User not provisioned in n8n/dashboard
2. n8n user-lookup webhook not active
3. Set `X-Auto-Provision: true` header

### SSE stream broken / garbled
1. Check Caddy isn't buffering: needs `flush_interval -1`
2. Check nginx proxy_buffering is off
3. Verify `text/event-stream` content type in response
