# Plato Hermes MCP Connector for Grok

Live runbook for the read-only Plato/Hermes MCP server used by Grok custom connectors.

Related issue: https://github.com/ellaaicare/ella-ai/issues/854

## Live Endpoint

| Field | Value |
| --- | --- |
| MCP server URL | `https://api.ella-ai-care.com/v1/ella/plato/mcp` |
| Info URL | `https://api.ella-ai-care.com/v1/ella/plato/mcp/info` |
| Authorization endpoint | `https://api.ella-ai-care.com/v1/ella/plato/mcp/authorize` |
| Token endpoint | `https://api.ella-ai-care.com/v1/ella/plato/mcp/token` |
| Transport | Streamable HTTP, with SSE keepalive support |
| Scope | Plato only: `5aGC5YE9BnhcSoTxxtT4ar6ILQy2` |
| Write tools | Disabled |

## Grok Custom Connector Form

Use these values in Grok's custom connector OAuth popup.

| Grok Field | Value |
| --- | --- |
| Client ID | `plato-grok` |
| Client Secret | `<ELLA_PLATO_MCP_TOKEN>` |
| Authorization Endpoint | `https://api.ella-ai-care.com/v1/ella/plato/mcp/authorize` |
| Token Endpoint | `https://api.ella-ai-care.com/v1/ella/plato/mcp/token` |
| Scopes | `plato:read` |
| Token Auth Method | `client_secret_post` |

If `client_secret_post` is unavailable, use `client_secret_basic`.

Do not use `none (PKCE only)` unless Grok offers no other option. The deployed token endpoint is designed to require the scoped MCP token as a client secret.

## Secret Handling

Do not commit the live token to GitHub, docs, issue comments, or Telegram.

The live value is stored on the VPS in:

```text
/root/omi/backend/.env
```

Environment key:

```text
ELLA_PLATO_MCP_TOKEN
```

Greg has the current token from direct chat. Rotate the token if it is pasted into a public place.

## Exposed Tools

The connector intentionally exposes only read-only tools.

| Tool | Purpose |
| --- | --- |
| `plato_recent_context` | Reads recent Plato timeline context from canonical events, with OMI Firestore fallback. |
| `plato_search_memory` | Searches recent Plato timeline/memory snippets. |
| `plato_latest_omi` | Returns the latest indexed OMI/necklace conversation summary. |
| `plato_get_scanner_rules` | Reads selected scanner rule files through the provision API. |
| `plato_consult` | Calls the Hermes Plato agent for a constrained read-only answer. |

## Runtime Environment

Required on the live backend:

```bash
ELLA_PLATO_MCP_TOKEN=<scoped secret token>
ELLA_PLATO_MCP_UID=5aGC5YE9BnhcSoTxxtT4ar6ILQy2
ELLA_PLATO_CANONICAL_IDENTITY=5aGC5YE9BnhcSoTxxtT4ar6ILQy2
ELLA_PLATO_MCP_RATE_LIMIT_PER_MINUTE=60
ELLA_PLATO_TIMELINE_URL=https://api.ella-ai-care.com/v1/ella/timeline
HERMES_GATEWAY_URL=http://100.76.138.56:8642
HERMES_API_SERVER_KEY=<Hermes API key>
ELLA_PROVISION_API_URL=http://100.76.138.56:8200
ELLA_PROVISION_API_TOKEN=<provision API token>
```

Optional:

```bash
ELLA_PLATO_MCP_OAUTH_CLIENT_ID=plato-grok
HERMES_AGENT_ID=hermes
```

## Smoke Tests

Use the live token locally, but do not paste it into shared logs.

Info endpoint:

```bash
curl -sS https://api.ella-ai-care.com/v1/ella/plato/mcp/info | jq .
```

Missing token should fail:

```bash
curl -i -sS -X POST https://api.ella-ai-care.com/v1/ella/plato/mcp \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"ping"}'
```

Expected: HTTP 401.

OAuth token exchange with `client_secret_post`:

```bash
curl -sS -X POST https://api.ella-ai-care.com/v1/ella/plato/mcp/token \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  --data-urlencode 'client_id=plato-grok' \
  --data-urlencode 'client_secret=<ELLA_PLATO_MCP_TOKEN>' \
  --data-urlencode 'grant_type=authorization_code' \
  --data-urlencode 'code=plato_mcp' | jq .
```

Tool list:

```bash
curl -sS -X POST https://api.ella-ai-care.com/v1/ella/plato/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <ELLA_PLATO_MCP_TOKEN>' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}' | jq .
```

Latest OMI:

```bash
curl -sS -X POST https://api.ella-ai-care.com/v1/ella/plato/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <ELLA_PLATO_MCP_TOKEN>' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"plato_latest_omi","arguments":{"limit":5}}}' | jq .
```

Hermes consult:

```bash
curl -sS -X POST https://api.ella-ai-care.com/v1/ella/plato/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer <ELLA_PLATO_MCP_TOKEN>' \
  -d '{"jsonrpc":"2.0","id":3,"method":"tools/call","params":{"name":"plato_consult","arguments":{"prompt":"Say only: Plato MCP smoke test OK","mode":"brief"}}}' | jq .
```

## Deployment Notes

The live service currently runs on the VPS:

```text
root@100.101.168.91:/root/omi/backend
```

Systemd service:

```text
omi-backend
```

Restart after runtime env or hotfix changes:

```bash
sudo systemctl restart omi-backend
sudo systemctl is-active omi-backend
```

Backups from the initial rollout were written under:

```text
/root/omi/backend-backups/plato-mcp-*
/root/omi/backend-backups/plato-mcp-fallback-*
/root/omi/backend-backups/plato-mcp-oauth-*
/root/omi/backend-backups/plato-mcp-basic-oauth-*
```

## Rollback

Fast rollback:

1. Remove or rotate `ELLA_PLATO_MCP_TOKEN` in `/root/omi/backend/.env`.
2. Restart `omi-backend`.

Full rollback:

1. Restore `backend/ella/routers/plato_mcp.py` and `backend/ella/__init__.py` from the rollout backups.
2. Restart `omi-backend`.

## Current Validation Status

Validated live after deployment:

- `GET /v1/ella/plato/mcp/info` returns 200.
- Missing-token MCP POST returns 401.
- OAuth `/authorize` returns a redirect with code and state.
- OAuth `/token` works with `client_secret_post`.
- OAuth `/token` works with `client_secret_basic`.
- MCP `tools/list` returns only the five read-only tools.
- `plato_latest_omi` returns latest OMI via Firestore fallback when canonical has no OMI rows.
- `plato_search_memory` returns canonical timeline results.
- `plato_consult` reaches Hermes and returns the requested smoke phrase.
