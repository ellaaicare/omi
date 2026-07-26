# Isolated Voice Proxy Authentication

The public Ella voice socket authenticates with a short-lived OMI session JWT.
For every context, search, and agent-tool call, the proxy forwards both:

- `Authorization: Bearer <voice-session-jwt>`
- `X-Ella-Voice-Proxy-Token: <server-service-secret>`

The JWT is issued by `POST /v1/voice/session` and includes `iss=omi-backend`,
`aud=ella-voice-proxy`, `sub=<firebase-uid>`, `uid=<same-firebase-uid>`, `jti`,
provider, voice mode, isolated-runtime state, issue time, and expiry. OMI rejects
missing, invalid, expired, cross-UID, stale-runtime, and service-auth requests
before any user data lookup. Service-secret comparison is constant-time.

For isolated users, OMI resolves the active healthy Hermes runtime binding and
uses only its receipt `agent_id`. Workspace reads/searches go through Hermes
8210 with its server bearer and `X-Ella-Owner-Uid`. Bounded `ask_ella` calls go
through the owner-checked 8210 runtime chat route. Responses contain no gateway,
workspace, Honcho, provider, or service credentials.

## Required Server Configuration

OMI backend:

```text
ELLA_SESSION_SECRET=<shared session signing secret>
ELLA_VOICE_PROXY_SERVICE_TOKEN=<new independent proxy service secret>
ELLA_ALLOW_LEGACY_VOICE_SESSION_TOKENS=false
ELLA_HERMES_PROVISION_API_URL=http://<mac-mini-tailscale-ip>:8210
ELLA_HERMES_PROVISION_API_TOKEN=<8210 internal service token>
```

Voice proxy:

```text
ELLA_SESSION_SECRET=<same session signing secret>
ELLA_VOICE_PROXY_SERVICE_TOKEN=<same independent proxy service secret>
ELLA_ALLOW_LEGACY_VOICE_SESSION_TOKENS=false
```

Hermes 8210 keeps its runtime gateway key server-side. Native clients receive
only the short-lived session JWT and public WebSocket URL.

## Rollout And Rollback

1. Deploy the 8210 owner-bound runtime route, then OMI's modern token
   issuance/authentication while canary enforcement remains off.
2. Wait the full configured old-token lifetime, or require clients to reconnect
   and obtain a modern token.
3. Confirm `ELLA_ALLOW_LEGACY_VOICE_SESSION_TOKENS=false` on both services,
   configure the proxy dual headers, and deploy OMI/proxy enforcement together.
4. Keep `ELLA_RUNTIME_BINDINGS_ENABLED`, `ELLA_ISOLATED_VOICE_ROUTING_ENABLED`,
   and UID allowlists unchanged/off globally.
5. Canary one disposable UID and prove context/search/tool success plus UID-B
   denial before enabling isolated voice for that UID.

Rollback by removing the canary UID from isolated voice routing. Do not restore
tokenless public sockets, legacy voice-session JWTs, or V4-to-OpenClaw
fallback. A separate hardware path, if needed, must use its own reviewed
trusted-device token exchange.
