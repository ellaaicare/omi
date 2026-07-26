# Ella Voice Canary Control Plane

Issue: `ellaaicare/ella-ai#1113` (requirements frozen by
`ellaaicare/ella-ai#1112` and aligned with `ellaaicare/ella-ai#1065`).

## Contract

The Phase-1 canary is manually granted to 5–10 known users. Provider
credentials remain server-side. Passive capture/transcription is not metered;
only realtime V2V sessions use this control plane.

Default active entitlement:

- 45 realtime voice minutes/day
- 12 realtime voice hours/month
- 20 minutes/session
- one concurrent session
- warning at 80%; hard stop at 100%
- provider and mode allowlists
- 120 MB total PCM bytes/session and 6 MB/minute

`GET /v1/voice/entitlement` returns the stable app contract:

```json
{
  "status": "active",
  "plan": "canary",
  "revision": 3,
  "quota": {
    "daily_used_s": 0,
    "daily_limit_s": 2700,
    "monthly_used_s": 0,
    "monthly_limit_s": 43200,
    "max_session_s": 1200,
    "max_concurrent": 1,
    "soft_limit_ratio": 0.8,
    "resets_at": "UTC timestamp",
    "monthly_resets_at": "UTC timestamp"
  }
}
```

Typed issuance/termination states are `no_entitlement`, `invited`, `suspended`,
`revoked`, `expired`, `quota_daily`, `quota_monthly`, `cost_daily`,
`cost_monthly`, `concurrent`, `session_max`, `audio_limit`,
`audio_rate_limited`, `rate_limited`, `provider_not_allowed`,
`model_not_allowed`, `mode_not_allowed`, `voice_disabled`, `user_disabled`,
`provider_disabled`, `entitlement_stale`, and `voice_policy_unavailable`.

## Deployment order

1. Apply `backend/migrations/008_create_voice_canary_controls.sql`.
2. Configure `ELLA_VOICE_ALERT_WEBHOOK_URL` and provider cost estimates.
3. Grant the initial disposable test account, then the named canary UIDs.
4. Deploy OMI's modern token issuer with
   `ELLA_VOICE_CANARY_ENFORCEMENT_ENABLED=false` and
   `ELLA_SESSION_EXPIRY_MINUTES=25`.
5. Wait the full 25-minute legacy-token window, or require clients to reconnect
   and obtain a modern token.
6. Confirm `ELLA_ALLOW_LEGACY_VOICE_SESSION_TOKENS=false` on OMI and the proxy,
   then deploy both services with canary enforcement enabled.
7. Exercise denial, one successful session, concurrency, daily hard stop,
   global/user/provider switches, ledger rollup, and alert delivery.

Do not deploy step 4 before the migration and grants. The secure default denies
every UID without an active entitlement. There is no proxy-first compatibility
bridge because the canary control plane requires signed session ID, correlation
ID, and entitlement revision claims.

## One-operator commands

Run from `backend/` with the production PostgreSQL environment loaded. Commands
print structured JSON without secrets.

```bash
python scripts/voice_canary_admin.py grant FIREBASE_UID
python scripts/voice_canary_admin.py show FIREBASE_UID
python scripts/voice_canary_admin.py suspend FIREBASE_UID --note "Operator hold"
python scripts/voice_canary_admin.py revoke FIREBASE_UID

python scripts/voice_canary_admin.py kill global on --reason "Canary rollback"
python scripts/voice_canary_admin.py kill global off
python scripts/voice_canary_admin.py kill user on --value FIREBASE_UID
python scripts/voice_canary_admin.py kill user off --value FIREBASE_UID
python scripts/voice_canary_admin.py kill provider on --value grok-voice
python scripts/voice_canary_admin.py kill provider off --value grok-voice
```

Changing an entitlement or switch increments its revision. The proxy heartbeat
therefore terminates already-connected sessions after suspend/revoke/switch,
not just new sessions.

## Ledger and rollup evidence

The ledger stores no transcript text or audio. Terminal events contain internal
UID/session/correlation IDs, entitlement revision, provider/model/mode,
timestamps, connection/audio seconds, byte totals, tool/reconnect counts,
provider request IDs, termination/error codes, and estimated/reconciled cost.

```sql
SELECT event_type, normalized_error_code, COUNT(*)
FROM voice_usage_events
WHERE created_at >= NOW() - INTERVAL '1 day'
GROUP BY 1, 2
ORDER BY 1, 2;

SELECT
  uid,
  SUM(connection_s) FILTER (
    WHERE ended_at >= date_trunc('day', NOW() AT TIME ZONE 'UTC')
  ) AS daily_voice_s,
  SUM(connection_s) FILTER (
    WHERE ended_at >= date_trunc('month', NOW() AT TIME ZONE 'UTC')
  ) AS monthly_voice_s,
  SUM(estimated_cost_microusd) AS estimated_cost_microusd
FROM voice_usage_events
WHERE event_type IN ('session_completed', 'session_terminated')
GROUP BY uid;
```

The proxy posts absolute counters, and PostgreSQL uses `GREATEST`, so heartbeat
retries cannot double-count bytes/tools. Only one terminal event is inserted
because completion atomically deletes the active lease.

## Alerts and rollback

- Auth failures are counted in a five-minute proxy window; the threshold sends
  one deduplicated webhook alert.
- Session cost above `ELLA_VOICE_SPEND_ANOMALY_MICROUSD` sends a pseudonymous
  alert.
- Failed alert delivery is logged with only a UID hash and exception type.
- Immediate rollback: global kill switch on. This stops new issuance/accepts
  and ends live sessions on their next heartbeat/turn boundary.
