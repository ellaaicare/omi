"""Typed, provider-neutral semantic Guardian playback-source classifier.

Product rule: whether a re-heard Whisper is Ella's own audio played back
through the mic (an "echo"), the owner's live speech, someone else talking,
TV/media, or a mix, is a SEMANTIC judgment made by an LLM over the
transcript plus the owner's own played-ledger candidates. It is never
decided by text/regex/fuzzy matching — a re-heard Whisper commonly comes
back reworded, partial, or mixed with real speech, which no string
comparison can reliably tell apart from a coincidentally similar thing the
owner actually said.

Provider order: a Jev Decisions adapter (OpenRouter `/api/alpha/decisions`)
first, then a generic chat-completion LLM fallback. Both are optional and
keyed from the environment; with no key configured for either, the
classifier is disabled and every call fails open.

Every failure mode — no provider key, no candidates to compare against,
provider timeout, provider/network error, an invalid or non-extractive
response — FAILS OPEN: it is treated as ordinary (possibly ambiguous) user
speech, never suppressed, and can never by itself create a Whisper. Only an
explicit, schema-valid, non-"unclear" classification can mark a transcript
as confirmed Ella playback.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

from database.honcho_attestation import authority_credential

SCHEMA_VERSION = "guardian_playback_source_v1"
_VALID_SOURCES = {"ella_playback", "live_user", "other_person", "tv_media", "mixed", "unclear"}

REASON_NO_KEY = "no_provider_key"
REASON_NO_CANDIDATES = "no_candidates"
REASON_TIMEOUT = "provider_timeout"
REASON_PROVIDER_ERROR = "provider_error"
REASON_INVALID_SCHEMA = "invalid_schema"

_JEV_API_KEY = authority_credential("JEV_DECISIONS_API_KEY", strip=False) or authority_credential(
    "OPENROUTER_API_KEY", strip=False
)
_JEV_API_BASE = os.getenv("JEV_DECISIONS_API_BASE", "https://openrouter.ai/api/alpha/decisions")
_JEV_MODEL = os.getenv("JEV_DECISIONS_MODEL", "jev/guardian-playback-source-v1")

_OPENROUTER_KEY = authority_credential("OPENROUTER_API_KEY", strip=False)
_FALLBACK_API_KEY = _OPENROUTER_KEY or authority_credential("XAI_API_KEY", strip=False)
_FALLBACK_API_BASE = "https://openrouter.ai/api/v1" if _OPENROUTER_KEY else "https://api.x.ai/v1"
_FALLBACK_MODEL = os.getenv(
    "ELLA_GUARDIAN_ECHO_FALLBACK_MODEL",
    "x-ai/grok-4.1-fast" if _OPENROUTER_KEY else "grok-4-1-fast-non-reasoning",
)

_CLASSIFIER_TIMEOUT_SECONDS = float(os.getenv("ELLA_GUARDIAN_ECHO_CLASSIFIER_TIMEOUT_SECONDS", "4.0"))

_FALLBACK_SYSTEM_PROMPT = """You classify one short transcript window from a hearing-support companion app.

The owner's device may have just played one or more short "Whisper" audio clips out loud (Ella's own voice), which the microphone can pick back up, reworded, partial, or mixed with the owner's own real speech.

You are given:
- the transcript window to classify
- the owner's own recently-PLAYED Whisper candidates (each with a playback_id and its exact text)

Decide, for the transcript window as a whole:
- is_ella_playback: true if any part of the transcript is Ella's own audio being re-heard
- source: exactly one of "ella_playback", "live_user", "other_person", "tv_media", "mixed", "unclear"
- contains_additional_live_speech: true if there is real speech beyond an echo of a candidate
- matched_playback_ids: playback_id values (from the candidates given) that this transcript echoes
- live_speech_spans: the exact substrings of the transcript window that are real live speech (character-for-character from the transcript — never rewritten, translated, or paraphrased); empty if none
- confidence: 0.0-1.0
- reason_code: a short snake_case label for your decision

If you cannot confidently extract live_speech_spans as literal substrings of the transcript, return an empty list and set source to "unclear" rather than guessing.

Respond with ONLY a JSON object, no prose, matching exactly:
{"schema_version": "guardian_playback_source_v1", "is_ella_playback": bool, "source": "...", "contains_additional_live_speech": bool, "matched_playback_ids": [...], "live_speech_spans": [...], "confidence": 0.0, "reason_code": "..."}"""


@dataclass(frozen=True)
class EchoClassification:
    schema_version: str
    is_ella_playback: bool
    source: str
    contains_additional_live_speech: bool
    matched_playback_ids: list[str]
    live_speech_spans: list[str]
    confidence: float
    reason_code: str
    fail_open: bool

    @property
    def is_confirmed_echo(self) -> bool:
        """True only for an affirmative, schema-valid match against an owner Whisper.

        Any fail-open result is never confirmed, regardless of `source`.
        """
        return (
            not self.fail_open
            and self.is_ella_playback
            and self.source in ("ella_playback", "mixed")
            and bool(self.matched_playback_ids)
        )

    @property
    def validated_live_spans(self) -> list[str]:
        if self.fail_open:
            return []
        return list(self.live_speech_spans)

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "is_ella_playback": self.is_ella_playback,
            "source": self.source,
            "contains_additional_live_speech": self.contains_additional_live_speech,
            "matched_playback_ids": self.matched_playback_ids,
            "live_speech_spans": self.live_speech_spans,
            "confidence": self.confidence,
            "reason_code": self.reason_code,
        }


def _fail_open(reason: str) -> EchoClassification:
    return EchoClassification(
        schema_version=SCHEMA_VERSION,
        is_ella_playback=False,
        source="unclear",
        contains_additional_live_speech=True,
        matched_playback_ids=[],
        live_speech_spans=[],
        confidence=0.0,
        reason_code=reason,
        fail_open=True,
    )


def _validate_payload(payload: Any, *, transcript: str, known_playback_ids: set[str]) -> Optional[EchoClassification]:
    """Validate a provider response against the typed schema.

    Returns None (never raises) when the payload is malformed or its
    live_speech_spans are not literal, extractive substrings of the
    transcript window — callers must treat None as REASON_INVALID_SCHEMA
    and fail open.
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("schema_version") != SCHEMA_VERSION:
        return None

    source = payload.get("source")
    if source not in _VALID_SOURCES:
        return None

    is_ella_playback = payload.get("is_ella_playback")
    if not isinstance(is_ella_playback, bool):
        return None

    contains_additional_live_speech = payload.get("contains_additional_live_speech")
    if not isinstance(contains_additional_live_speech, bool):
        return None

    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        return None
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        return None

    matched_playback_ids = payload.get("matched_playback_ids")
    if not isinstance(matched_playback_ids, list) or not all(isinstance(v, str) for v in matched_playback_ids):
        return None
    matched_playback_ids = [pid for pid in matched_playback_ids if pid in known_playback_ids]

    live_speech_spans = payload.get("live_speech_spans")
    if not isinstance(live_speech_spans, list) or not all(isinstance(v, str) for v in live_speech_spans):
        return None
    # Extractive-only: every claimed span must be a literal substring of the
    # transcript we sent. A hallucinated/rewritten span means the model
    # could not ground its answer in the actual transcript, so the whole
    # response is untrustworthy — fail open rather than keep any of it.
    if any(span and span not in transcript for span in live_speech_spans):
        return None

    # Cross-field invariant: a claim of additional live speech (whether via
    # `source="mixed"` or `contains_additional_live_speech`) is only
    # trustworthy if it is backed by at least one non-blank extractive span.
    # Without one there is nothing to retain, so a downstream caller could
    # silently drop real speech instead of failing open — treat this as an
    # invalid response rather than trusting the unsupported claim.
    claims_additional_live_speech = source == "mixed" or contains_additional_live_speech
    has_non_blank_span = any(span.strip() for span in live_speech_spans)
    if claims_additional_live_speech and not has_non_blank_span:
        return None

    reason_code = payload.get("reason_code")
    if not isinstance(reason_code, str) or not reason_code:
        reason_code = "classified"

    return EchoClassification(
        schema_version=SCHEMA_VERSION,
        is_ella_playback=is_ella_playback,
        source=source,
        contains_additional_live_speech=contains_additional_live_speech,
        matched_playback_ids=matched_playback_ids,
        live_speech_spans=live_speech_spans,
        confidence=confidence,
        reason_code=reason_code,
        fail_open=source == "unclear",
    )


def _candidate_payload(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "playback_id": str(c["playback_id"]),
            "text": str(c.get("text") or ""),
            "started_at": c.get("started_at"),
            "completed_at": c.get("completed_at"),
            "duration_ms": c.get("duration_ms"),
        }
        for c in candidates
    ]


async def _call_jev_decisions(transcript: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=_CLASSIFIER_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            _JEV_API_BASE,
            headers={"Authorization": f"Bearer {_JEV_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": _JEV_MODEL,
                "schema_version": SCHEMA_VERSION,
                "input": {
                    "transcript": transcript,
                    "playback_candidates": _candidate_payload(candidates),
                },
            },
        )
    resp.raise_for_status()
    body = resp.json()
    return body.get("decision", body)


async def _call_fallback_llm(transcript: str, candidates: list[dict[str, Any]]) -> dict[str, Any]:
    user_prompt = (
        "Transcript window:\n"
        f"{transcript}\n\n"
        "Owner's recently-played Whisper candidates:\n"
        f"{_candidate_payload(candidates)!r}"
    )
    async with httpx.AsyncClient(timeout=_CLASSIFIER_TIMEOUT_SECONDS) as client:
        resp = await client.post(
            f"{_FALLBACK_API_BASE}/chat/completions",
            headers={"Authorization": f"Bearer {_FALLBACK_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": _FALLBACK_MODEL,
                "messages": [
                    {"role": "system", "content": _FALLBACK_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "max_tokens": 400,
                "temperature": 0.0,
                "response_format": {"type": "json_object"},
            },
        )
    resp.raise_for_status()
    content = resp.json()["choices"][0]["message"]["content"]
    return json.loads(content)


async def classify_playback_source(
    transcript: str,
    candidates: list[dict[str, Any]],
    *,
    uid: str = "",
) -> EchoClassification:
    """Classify whether `transcript` is (partly) Ella's own played-back audio.

    `candidates` must already be scoped to the authenticated owner's own
    recently-PLAYED ledger entries (see
    `guardian_playback_ledger.get_played_candidates`) — this function does
    not do any owner scoping of its own and trusts its caller for that.
    """
    if not _JEV_API_KEY and not _FALLBACK_API_KEY:
        return _fail_open(REASON_NO_KEY)
    if not candidates or not str(transcript or "").strip():
        return _fail_open(REASON_NO_CANDIDATES)

    known_playback_ids = {str(c["playback_id"]) for c in candidates}
    last_reason = REASON_PROVIDER_ERROR

    if _JEV_API_KEY:
        try:
            payload = await _call_jev_decisions(transcript, candidates)
        except httpx.TimeoutException:
            last_reason = REASON_TIMEOUT
        except httpx.HTTPError:
            last_reason = REASON_PROVIDER_ERROR
        except (ValueError, KeyError):
            last_reason = REASON_INVALID_SCHEMA
        else:
            result = _validate_payload(payload, transcript=transcript, known_playback_ids=known_playback_ids)
            if result is not None:
                _log_classification(uid, result)
                return result
            last_reason = REASON_INVALID_SCHEMA

    if _FALLBACK_API_KEY:
        try:
            payload = await _call_fallback_llm(transcript, candidates)
        except httpx.TimeoutException:
            last_reason = REASON_TIMEOUT
        except httpx.HTTPError:
            last_reason = REASON_PROVIDER_ERROR
        except (ValueError, KeyError):
            last_reason = REASON_INVALID_SCHEMA
        else:
            result = _validate_payload(payload, transcript=transcript, known_playback_ids=known_playback_ids)
            if result is not None:
                _log_classification(uid, result)
                return result
            last_reason = REASON_INVALID_SCHEMA

    result = _fail_open(last_reason)
    _log_classification(uid, result)
    return result


def _log_classification(uid: str, result: EchoClassification) -> None:
    """Log only ids/status — never the transcript or candidate/playback text."""
    print(
        f"[FLOW:GUARDIAN-ECHO-CLASSIFIER] uid={uid} source={result.source} "
        f"confirmed={result.is_confirmed_echo} fail_open={result.fail_open} "
        f"matched={len(result.matched_playback_ids)} confidence={result.confidence:.2f} "
        f"reason={result.reason_code}",
        flush=True,
    )
