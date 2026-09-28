# Guardian Echo Guard Node Contract (n8n)

Issue: ellaaicare/ella-ai#600 (Whispers playback ledger + semantic echo)

**Design update: the echo-source judgment is executed in the OMI backend,
not in n8n.** `send_to_scanner` (`utils/ella/scanner.py`) now calls
`classify_playback_source` itself, before a transcript window is ever
dispatched to the scanner webhook, whenever owner-bound PLAYED ledger
candidates exist. A confirmed echo is tagged as Ella's own output and is
**never dispatched** to n8n at all (so it can never create a Whisper/queue
row); a `mixed` result dispatches only the classifier-validated live
speech spans; `unclear`, a provider error, or a timeout fails open and
dispatches the original window unchanged. This is the same fail-open
contract the node contract below originally specified — it is now
enforced by an executable, tested backend code path
(`utils/ella/scanner.py::send_to_scanner`,
`ella/services/guardian_echo_classifier.py::classify_playback_source`)
instead of being documentation for the private n8n workflow to implement.

**No n8n change is required.** The Echo Guard node in the private workflow
repo receives exactly what it always has — a scanner webhook call with a
transcript window — except a window the backend already confirmed as pure
Ella playback echo simply never arrives, and a `mixed` window arrives with
only its live-speech portion. The node does not need to call the
classifier itself, does not need to know `playback_candidates` was ever
attached, and requires no change to keep working correctly. The rest of
this document is kept as the schema/contract reference for
`guardian_playback_source_v1` and for any future non-OMI caller that also
needs to reason about this data — the backend's own classifier call is the
canonical, executable implementation of everything described below.

## What changed

The Echo Guard node used to make its own similarity/keyword-match verdict
on whether the current transcript window was Ella's own audio being
re-heard through the mic. That verdict is now made ONLY by the typed
semantic classifier described below — the backend evaluates it before
dispatch, so the node never needs to compute one on its own, whether by
text/regex/fuzzy matching or by calling the classifier a second time.

## What the backend attaches (for reference / any future caller)

`send_to_scanner` selects the owner's own recently-PLAYED Guardian Whisper
ledger entries (see `ella/services/guardian_playback_ledger.py`), by
time/route proximity, and attaches them to the payload as
`playback_candidates` *before* running the classification described below.
`playback_candidates` may still be present on the dispatched payload for
observability, but by the time n8n sees a window, the backend has already
applied the judgment — a confirmed echo never reaches this payload at all,
and a mixed window's `segments` already contains only the live spans:

```json
{
  "uid": "omi-user-id",
  "conversation_id": "conversation-uuid",
  "trace_id": "conversation-uuid",
  "segments": [{"speaker": "SPEAKER_0", "text": "transcript text", "stt_source": "deepgram"}],
  "playback_candidates": [
    {
      "playback_id": "guardian_abc123",
      "text": "Hi Greg, I heard my name. I'm here with you.",
      "started_at": "2026-09-28T15:04:05.000000+00:00",
      "completed_at": "2026-09-28T15:04:07.500000+00:00",
      "duration_ms": 2500
    }
  ]
}
```

`playback_candidates` may be an empty list. Selecting a candidate by
time/route proximity is never itself a verdict — presence of a candidate
does not mean the transcript is an echo of it. Only the classifier's
judgment decides that, and (as of this change) that judgment is applied by
the backend before dispatch, not by anything reading this payload.

## What the node must do

**Nothing new.** The node does not need to call the classifier, does not
need to branch on `playback_candidates`, and does not need any change for
this contract — the backend has already applied steps 1–5 below before the
node's webhook ever fires. This section is kept as the specification of
what the backend's `send_to_scanner` / `classify_playback_source` call
actually does, in case a future non-OMI caller needs to reproduce the same
judgment:

1. If there are no owner-bound PLAYED ledger candidates, skip the
   classifier call and treat the transcript as ordinary (possibly
   ambiguous) user speech. The classifier fails open on that case anyway
   (see below), so calling it would be wasted latency.
2. Otherwise call the typed classifier (Jev Decisions via OpenRouter
   `/api/alpha/decisions`, generic LLM chat-completion fallback) with
   exactly the transcript window plus the candidates — never any other
   user's data, never the raw scanner segments beyond this window.
3. Validate the response against the schema below. On timeout, a
   non-2xx/network error, or a schema-invalid response (including
   non-extractive `live_speech_spans`, or a `mixed`/
   `contains_additional_live_speech` claim with no non-blank extractive
   span backing it), FAIL OPEN: treat the window as ordinary user speech,
   do not suppress it, and do not let it single-handedly create a Whisper.
4. On a confirmed echo with `source: "ella_playback"` (`is_ella_playback:
   true`, `matched_playback_ids` non-empty): tag the window as Ella's own
   output and do not dispatch it as user speech at all. It must never
   retrigger a Whisper and must never count toward repetition/confusion
   heuristics.
5. On `source: "mixed"`: only the validated `live_speech_spans` continue
   downstream as real user speech — dispatch just that portion. The rest
   of the window stays tagged as Ella output per (4). If spans can't be
   validated as literal substrings of the transcript, the whole response
   is invalid per (3) — fail open on the original, untouched transcript
   instead of guessing.

## Output schema (`guardian_playback_source_v1`)

```json
{
  "schema_version": "guardian_playback_source_v1",
  "is_ella_playback": false,
  "source": "ella_playback",
  "contains_additional_live_speech": false,
  "matched_playback_ids": ["guardian_abc123"],
  "live_speech_spans": [],
  "confidence": 0.92,
  "reason_code": "exact_echo_of_recent_playback"
}
```

`source` is exactly one of: `ella_playback`, `live_user`, `other_person`,
`tv_media`, `mixed`, `unclear`.

This is the same schema the Python-side classifier
(`ella/services/guardian_echo_classifier.py::classify_playback_source`)
validates and returns as an `EchoClassification`. If the node calls a
different endpoint than the backend's own classifier for symmetry with the
private-repo workflow, it MUST validate against this exact schema — not a
looser or renamed variant — so the two call sites agree on what "confirmed
echo" means.

## Fresh-wake regex fix

The fresh-wake-phrase detector's `\s` (whitespace) escaping in the node's
expression was broken (an unescaped backslash that a JSON-string workflow
export silently mangled into a literal `s`, matching a literal `s` rather
than any whitespace character). Fix the escaping so it matches whitespace
correctly. This regex still has NO suppressing authority over dispatch —
same as `should_suppress_guardian_echo`'s removal on the backend side, a
regex here may only be used to detect a wake-prefix shape for immediate
dispatch, never to veto or classify echo.

## Webhook authentication ordering

The scanner webhook handler must validate `X-Ella-Scanner-Webhook-Key`
(the node's inbound trust boundary) BEFORE any uid lookup, database read,
provider call, trace write, or branching logic — including before deciding
whether to call the classifier. An invalid or missing key must short-circuit
to a 401/403 with no other side effects.

## No text in logs, customData, or error receipts

Never place `playback_candidates[].text`, the transcript window, or any
derived quote from either into an n8n execution log, `customData`, or an
error/trace receipt. The classifier call itself (the functional payload) is
the one place this text is allowed to travel — everything else must log
only ids, counts, and status/reason codes, exactly like
`ella/services/guardian_echo_classifier.py::_log_classification`.
