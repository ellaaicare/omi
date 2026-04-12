# Guardian Escalation Trace Contract

Issue: ellaaicare/ella-ai#600

This contract links scanner decisions, guardian queue insertion, iOS playback,
and fallback delivery attempts into one durable trace in
`guardian_pipeline_events`.

## Trace ID

Use `trace_id` as the cross-service correlation key.

- For transcript-driven scanner flows, `trace_id` MUST be the OMI
  `conversation_id`.
- If no conversation exists, use the queue item id or a generated diagnostic id.
- Do not use `unknown` as a real trace id for production aggregation.

## Backend Scanner Payload

The OMI backend sends this shape to the scanner webhook:

```json
{
  "uid": "omi-user-id",
  "conversation_id": "conversation-uuid",
  "trace_id": "conversation-uuid",
  "device_type": "omi",
  "segments": [
    {
      "speaker": "SPEAKER_0",
      "text": "transcript text",
      "stt_source": "deepgram"
    }
  ],
  "trace": {
    "id": "conversation-uuid",
    "schema_version": "guardian-pipeline-v1",
    "source": "omi-backend",
    "contract": "ella-ai#600"
  }
}
```

The backend also writes a best-effort `scanner_dispatched` trace event without
including raw transcript text.

## n8n Fallback Callback Contract

Issue #598 should use:

`POST /v1/ella/guardian/trace/log`

```json
{
  "trace_id": "conversation-uuid",
  "uid": "omi-user-id",
  "stage": "fallback_imessage_sent",
  "status": "success",
  "latency_ms": 1234,
  "metadata": {
    "queue_item_id": "guardian_abc123",
    "provider": "bluebubbles",
    "attempt": 1,
    "reason": "guardian_mode_off"
  }
}
```

Recommended n8n stages:

- `scanner_classified`
- `escalation_decided`
- `tts_requested`
- `tts_failed`
- `fallback_imessage_attempted`
- `fallback_imessage_sent`
- `fallback_email_attempted`
- `fallback_email_sent`
- `fallback_caregiver_attempted`
- `fallback_caregiver_sent`
- `fallback_failed`

Use `status` values `success`, `error`, `timeout`, `rejected`, or `skipped`.

## Guardian Queue Contract

`POST /v1/ella/guardian/enqueue` accepts `metadata.trace_id`; if omitted, the
backend falls back to `metadata.conversation_id` or the queue item id.

The backend persists these stages:

- `queue_inserted`
- `queue_rejected`
- `audio_consumed`

`GET /v1/ella/guardian/next-audio` returns:

```json
{
  "url": "https://...",
  "id": "guardian_abc123",
  "trace_id": "conversation-uuid",
  "priority": "urgent",
  "message": "spoken message",
  "trigger_type": "scanner-escalation",
  "metadata": {
    "trace_id": "conversation-uuid",
    "queue_item_id": "guardian_abc123"
  }
}
```

## iOS Playback Receipt Contract

Issue #599 should call:

`POST /v1/ella/guardian/playback-event`

```json
{
  "uid": "omi-user-id",
  "queue_item_id": "guardian_abc123",
  "trace_id": "conversation-uuid",
  "event_type": "started",
  "port_type": "Speaker",
  "port_name": "iPhone Speaker",
  "device_uid": "AVAudioSessionPortDescription.uid",
  "duration_ms": 0,
  "metadata": {
    "app_state": "foreground"
  }
}
```

Required fields:

- `uid`
- `event_type`: `started`, `completed`, or `failed`
- `queue_item_id` or `trace_id`
- `port_type`

The backend persists stages named `ios_playback_started`,
`ios_playback_completed`, and `ios_playback_failed`.

For `failed`, include the failure reason in `metadata.error`.

## Trace Readback

`GET /v1/ella/guardian/trace/{trace_id}` returns ordered stages plus aggregate
latency. This is the readback endpoint for #589 canary proof.
