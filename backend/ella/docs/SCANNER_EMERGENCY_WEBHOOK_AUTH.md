# Scanner and Emergency Webhook Authority

The Ella backend uses separate transport credentials for its two n8n webhook
trust domains:

| Route | Backend environment | Request header |
|---|---|---|
| `/webhook/scanner-agent` | `ELLA_SCANNER_WEBHOOK_KEY` | `X-Ella-Scanner-Webhook-Key` |
| `/webhook/emergency-alert` | `ELLA_EMERGENCY_WEBHOOK_KEY` | `X-Ella-Emergency-Webhook-Key` |

The values are independent secrets. They must not be reused as Firebase,
Guardian loopback, or provider credentials. The backend fails closed before
webhook egress when the corresponding credential is absent.

The emergency endpoint derives eligible contacts from the authenticated
owner's backend records. Caller-supplied contacts and audio URLs are accepted
only for backward request compatibility and are never forwarded. A successful
push to the owner is not reported as confirmed caregiver delivery.

## Rollout order

1. Create inactive n8n successor workflows with header authentication and no
   live caregiver targets.
2. Configure the two secret values in n8n credentials and the matching backend
   environment variables without printing them.
3. Independently review the exact backend and workflow artifacts.
4. Activate and verify the scanner successor with synthetic traffic.
5. Keep emergency delivery disabled until the `emergency-alert` successor has
   a reviewed dry-run path, server-owned contact handling, idempotency, and an
   explicit no-real-send acceptance test.

The backend source alone does not make either live n8n workflow authenticated
or create the missing `emergency-alert` target.
