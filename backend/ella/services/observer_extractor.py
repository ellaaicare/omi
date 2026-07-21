"""Observer candidate extraction from canonical ledger events.

This module is intentionally separate from the proposal runner. Extraction can
use heuristics or a model, but the runner remains the only place that validates,
dedupes, logs, and proposes mutations.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ella.services.observer import ObserverCandidate, structured_candidate_extractor
from ella.services import runtime_resolver

MAX_EXTRACTOR_EVENTS = 60
MAX_EVENT_TEXT_CHARS = 1800
SUPPORTED_EXTRACTOR_MODES = {"structured", "heuristic", "hermes"}
HEURISTIC_USER_CHANNELS = {"ios_chat", "ios_voice", "imessage", "telegram"}

_REMEMBER_RE = re.compile(
    r"\b(?:(?:please\s+)?remember|note(?:\s+that)?|keep track of|log this|save this)\b(?P<fact>.*)",
    re.IGNORECASE | re.DOTALL,
)
_CORRECTION_RE = re.compile(r"\b(?:actually|correction|it was|that was|the correct)\b(?P<fact>.*)", re.I | re.S)
_REMINDER_RE = re.compile(r"\b(?:remind me|reminder|don't let me forget)\b(?P<fact>.*)", re.IGNORECASE | re.DOTALL)
_SCANNER_RE = re.compile(
    r"\b(?:watch for|listen for|scan for|alert me|tell me if|notify me if|guardian should|necklace should)\b(?P<fact>.*)",
    re.IGNORECASE | re.DOTALL,
)
_LOW_SIGNAL_RE = re.compile(r"^\W*(?:ok|okay|yeah|yes|no|thanks|thank you|no problem|sixteen|not white)\W*$", re.I)


@dataclass
class ExtractionResult:
    candidates_by_event_id: dict[str, list[ObserverCandidate]] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def normalize_extractor_mode(mode: str | None) -> str:
    value = (mode or "").strip().lower() or _env("ELLA_OBSERVER_EXTRACTOR_MODE", "structured").lower()
    if value not in SUPPORTED_EXTRACTOR_MODES:
        return "structured"
    return value


def _event_id(event: dict[str, Any]) -> str:
    return str(event.get("event_id") or "")


def _event_text(event: dict[str, Any], max_chars: int = MAX_EVENT_TEXT_CHARS) -> str:
    text = str(event.get("text") or "").replace("\x00", "").strip()
    if len(text) > max_chars:
        return text[:max_chars].rstrip()
    return text


def _event_title(event: dict[str, Any]) -> str:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    for key in ("title", "summary_title", "conversation_title"):
        value = metadata.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()[:160]
    source_ref = event.get("source_ref") if isinstance(event.get("source_ref"), dict) else {}
    value = source_ref.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()[:160]
    return ""


def _is_low_signal(event: dict[str, Any]) -> bool:
    text = _event_text(event, max_chars=280)
    if not text or _LOW_SIGNAL_RE.match(text):
        return True
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    signal = metadata.get("ella_signal") if isinstance(metadata.get("ella_signal"), dict) else {}
    salience = str(metadata.get("salience") or signal.get("salience") or "").lower()
    if salience in {"none", "low"} and len(text) < 80:
        return True
    return False


def _is_synthetic_or_test_event(event: dict[str, Any]) -> bool:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    source_ref = event.get("source_ref") if isinstance(event.get("source_ref"), dict) else {}
    if metadata.get("synthetic") is True or metadata.get("test") is True:
        return True
    for value in (
        metadata.get("test"),
        metadata.get("source"),
        metadata.get("fixture"),
        event.get("provider"),
        event.get("event_id"),
        source_ref.get("source_id"),
    ):
        text = str(value or "").lower()
        if any(marker in text for marker in ("ella_memory_e2e", "ella-memory-e2e", "automated_e2e_smoke")):
            return True
    return False


def _is_memory_instruction(text: str, match: re.Match[str]) -> bool:
    lower = text.lower().strip()
    prefix = lower[: match.start()].strip()
    fact = (match.group("fact") or "").strip(" .:-\n\t").lower()
    if re.search(r"\b(?:do you|can you|could you|what do you|what did i|did i)\s+$", prefix):
        return False
    if prefix.endswith(("what", "where", "when", "why", "how")):
        return False
    if fact.startswith(("what ", "who ", "when ", "why ", "how ", "anything ", "the last ")):
        return False
    if "?" in text and not re.search(
        r"\b(?:please remember|remember that|remember this|remember my|remember where)\b", lower
    ):
        return False
    return bool(fact)


def _candidate(
    *,
    proposal_type: str,
    title: str,
    description: str,
    requested_change: dict[str, Any],
    event: dict[str, Any],
    reason: str,
    confidence: float,
) -> ObserverCandidate:
    return ObserverCandidate(
        proposal_type=proposal_type,
        title=title[:240],
        description=description[:4000],
        requested_change=requested_change,
        target={"profile_uid": event.get("uid"), "canonical_identity": event.get("canonical_identity")},
        reason=reason,
        confidence=confidence,
        evidence=[
            {
                "event_id": event.get("event_id"),
                "channel": event.get("channel"),
                "provider": event.get("provider"),
                "started_at": event.get("started_at"),
                "extractor": "heuristic",
            }
        ],
    )


def heuristic_candidate_extractor(event: dict[str, Any]) -> list[ObserverCandidate]:
    """High-precision local extractor for explicit user commands/corrections."""
    if not isinstance(event, dict) or str(event.get("role") or "").lower() not in {"user", "speaker"}:
        return []
    if _is_synthetic_or_test_event(event):
        return []
    if str(event.get("channel") or "") not in HEURISTIC_USER_CHANNELS:
        return []
    if _is_low_signal(event):
        return []

    text = _event_text(event)
    title = _event_title(event)
    candidates: list[ObserverCandidate] = []

    remember = _REMEMBER_RE.search(text)
    if remember and _is_memory_instruction(text, remember):
        fact = (remember.group("fact") or text).strip(" .:-\n\t")
        memory = fact or text
        candidates.append(
            _candidate(
                proposal_type="memory_note",
                title=f"Remembered context: {memory[:80]}",
                description=f"The user explicitly asked Ella to remember this: {memory}",
                requested_change={"memory": memory, "source_text": text, "category": "explicit_user_memory"},
                event=event,
                reason="User explicitly asked Ella to remember durable context.",
                confidence=0.88,
            )
        )

    correction = _CORRECTION_RE.search(text)
    if correction and len(text) >= 12:
        fact = (correction.group("fact") or text).strip(" .:-\n\t")
        candidates.append(
            _candidate(
                proposal_type="summary_correction",
                title=f"User correction: {(fact or title or text)[:90]}",
                description=f"The user provided a correction that may affect summaries or memory: {text}",
                requested_change={"correction_text": text, "corrected_fact": fact, "source_title": title},
                event=event,
                reason="User explicitly supplied a correction.",
                confidence=0.86,
            )
        )

    reminder = _REMINDER_RE.search(text)
    if reminder:
        request_text = (reminder.group("fact") or text).strip(" .:-\n\t")
        candidates.append(
            _candidate(
                proposal_type="reminder_request",
                title=f"Reminder request: {(request_text or text)[:90]}",
                description=f"The user requested a reminder: {text}",
                requested_change={"reminder_text": text, "request": request_text},
                event=event,
                reason="User explicitly requested a reminder.",
                confidence=0.84,
            )
        )

    scanner = _SCANNER_RE.search(text)
    if scanner:
        rule_text = (scanner.group("fact") or text).strip(" .:-\n\t")
        candidates.append(
            _candidate(
                proposal_type="scanner_rule_change",
                title=f"Scanner rule request: {(rule_text or text)[:90]}",
                description=f"The user requested Guardian/scanner monitoring behavior: {text}",
                requested_change={"rule_text": text, "request": rule_text, "scope": "user_requested"},
                event=event,
                reason="User explicitly requested scanner or Guardian behavior.",
                confidence=0.82,
            )
        )

    return candidates


def _event_payload(event: dict[str, Any]) -> dict[str, Any]:
    metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
    signal = metadata.get("ella_signal") if isinstance(metadata.get("ella_signal"), dict) else {}
    return {
        "event_id": event.get("event_id"),
        "channel": event.get("channel"),
        "provider": event.get("provider"),
        "role": event.get("role"),
        "started_at": event.get("started_at"),
        "title": _event_title(event),
        "salience": metadata.get("salience") or signal.get("salience"),
        "tags": metadata.get("tags") or signal.get("tags") or [],
        "text": _event_text(event),
    }


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw)
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        start = raw.find("{")
        end = raw.rfind("}")
        if start < 0 or end <= start:
            raise
        value = json.loads(raw[start : end + 1])
    return value if isinstance(value, dict) else {}


def _candidate_from_model(raw: dict[str, Any], event_lookup: dict[str, dict[str, Any]]) -> ObserverCandidate | None:
    if not isinstance(raw, dict):
        return None
    evidence_event_ids = raw.get("evidence_event_ids") or raw.get("event_ids") or []
    if isinstance(evidence_event_ids, str):
        evidence_event_ids = [evidence_event_ids]
    if not isinstance(evidence_event_ids, list):
        evidence_event_ids = []
    event = event_lookup.get(str(evidence_event_ids[0])) if evidence_event_ids else None
    if not event:
        return None
    if str(event.get("channel") or "") != "omi" and str(event.get("role") or "").lower() not in {"user", "speaker"}:
        return None
    try:
        return ObserverCandidate(
            proposal_type=str(raw.get("proposal_type") or "").strip(),
            title=str(raw.get("title") or "").strip()[:240],
            description=str(raw.get("description") or "").strip()[:4000],
            requested_change=raw.get("requested_change") if isinstance(raw.get("requested_change"), dict) else {},
            target=raw.get("target") if isinstance(raw.get("target"), dict) else {},
            reason=str(raw.get("reason") or "model_candidate").strip()[:1000],
            confidence=float(raw.get("confidence") or 0.0),
            evidence=[
                {
                    "event_id": str(event_id),
                    "channel": event_lookup.get(str(event_id), {}).get("channel"),
                    "provider": event_lookup.get(str(event_id), {}).get("provider"),
                    "started_at": event_lookup.get(str(event_id), {}).get("started_at"),
                    "extractor": "hermes",
                }
                for event_id in evidence_event_ids
                if str(event_id) in event_lookup
            ],
        )
    except Exception:
        return None


async def hermes_candidate_extraction(
    events: list[dict[str, Any]],
    *,
    gateway_url: str | None = None,
    token: str | None = None,
    model: str | None = None,
    timeout_seconds: float = 45.0,
    limit: int = MAX_EXTRACTOR_EVENTS,
) -> ExtractionResult:
    """Ask Hermes for proposal candidates using a strict JSON-only contract."""
    selected = [
        event
        for event in events
        if isinstance(event, dict) and not _is_low_signal(event) and not _is_synthetic_or_test_event(event)
    ]
    selected = selected[-max(1, min(limit, MAX_EXTRACTOR_EVENTS)) :]
    if not selected:
        return ExtractionResult(metadata={"extractor": "hermes", "event_count": 0, "skipped": "no_signal_events"})

    url = (gateway_url or _env("HERMES_GATEWAY_URL", "http://100.76.138.56:8642")).rstrip("/")
    api_token = token if token is not None else _env("HERMES_API_SERVER_KEY", _env("API_SERVER_KEY", ""))
    model_name = model or _env("HERMES_MODEL", "plato-eval")
    if not api_token:
        return ExtractionResult(
            metadata={"extractor": "hermes", "event_count": len(selected), "error": "missing_hermes_token"}
        )

    payload_events = [_event_payload(event) for event in selected]
    system = (
        "You are Ella Observer's candidate extractor. Return only compact JSON. "
        "You do not mutate memory, scanner rules, reminders, summaries, or caregiver state. "
        "Find only high-confidence proposals from the supplied canonical events.\n\n"
        "Allowed proposal_type values: memory_note, profile_update, scanner_rule_change, "
        "reminder_request, summary_correction.\n"
        "Create candidates only for explicit durable user facts, explicit corrections, explicit "
        "reminder requests, or explicit scanner/Guardian instructions. Ignore TV, music, podcasts, "
        "news, background chatter, third-party claims, and ambiguous fragments unless the user makes "
        "them personally relevant. Every candidate must cite evidence_event_ids from the input.\n"
        "Schema: {\"candidates\":[{\"proposal_type\":\"memory_note\",\"title\":\"...\","
        "\"description\":\"...\",\"requested_change\":{},\"target\":{},\"reason\":\"...\","
        "\"confidence\":0.0,\"evidence_event_ids\":[\"...\"]}]}"
    )
    user = "Canonical events to inspect:\n" + json.dumps(payload_events, ensure_ascii=False, separators=(",", ":"))
    started = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=timeout_seconds) as client:
            response = await client.post(
                f"{url}/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_token}"},
                json={
                    "model": model_name,
                    "messages": [
                        {"role": "system", "content": system},
                        {"role": "user", "content": user},
                    ],
                    "temperature": 0,
                    "max_tokens": 1200,
                    "stream": False,
                },
            )
        response.raise_for_status()
        data = response.json()
        content = str((data.get("choices") or [{}])[0].get("message", {}).get("content") or "")
        parsed = _extract_json_object(content)
    except Exception as exc:
        return ExtractionResult(
            metadata={
                "extractor": "hermes",
                "event_count": len(selected),
                "latency_ms": int((time.monotonic() - started) * 1000),
                "error": type(exc).__name__,
                "error_detail": str(exc)[:240],
            }
        )

    event_lookup = {_event_id(event): event for event in selected if _event_id(event)}
    candidates_by_event_id: dict[str, list[ObserverCandidate]] = {}
    raw_candidates = parsed.get("candidates") if isinstance(parsed.get("candidates"), list) else []
    for raw in raw_candidates:
        candidate = _candidate_from_model(raw, event_lookup)
        if not candidate:
            continue
        for evidence in candidate.evidence or []:
            event_id = str(evidence.get("event_id") or "")
            if event_id:
                candidates_by_event_id.setdefault(event_id, []).append(candidate)
                break

    return ExtractionResult(
        candidates_by_event_id=candidates_by_event_id,
        metadata={
            "extractor": "hermes",
            "model": model_name,
            "event_count": len(selected),
            "candidate_count": sum(len(items) for items in candidates_by_event_id.values()),
            "latency_ms": int((time.monotonic() - started) * 1000),
        },
    )


async def build_extraction_result(
    events: list[dict[str, Any]],
    *,
    mode: str,
    uid: str = "",
    timeout_seconds: float = 45.0,
    limit: int = MAX_EXTRACTOR_EVENTS,
) -> ExtractionResult:
    normalized = normalize_extractor_mode(mode)
    if normalized == "structured":
        return ExtractionResult(metadata={"extractor": "structured_candidate_extractor"})
    if normalized == "heuristic":
        return _heuristic_extraction_result(events)

    heuristic = _heuristic_extraction_result(events)
    hermes_kwargs: dict[str, str] = {}
    if uid:
        if runtime_resolver.runtime_bindings_enabled(uid):
            runtime = await runtime_resolver.resolve_isolated_runtime(uid)
            if runtime is None:
                return ExtractionResult(
                    candidates_by_event_id=heuristic.candidates_by_event_id,
                    metadata={
                        "extractor": "hermes_plus_heuristic",
                        "heuristic": heuristic.metadata,
                        "hermes": {"error": "isolated_runtime_unavailable"},
                    },
                )
            hermes_kwargs = {
                "gateway_url": runtime.gateway_url,
                "token": runtime.gateway_token,
                "model": runtime.agent_id,
            }
    hermes = await hermes_candidate_extraction(
        events,
        timeout_seconds=timeout_seconds,
        limit=limit,
        **hermes_kwargs,
    )
    merged = {event_id: list(candidates) for event_id, candidates in heuristic.candidates_by_event_id.items()}
    for event_id, candidates in hermes.candidates_by_event_id.items():
        merged.setdefault(event_id, []).extend(candidates)
    return ExtractionResult(
        candidates_by_event_id=merged,
        metadata={
            "extractor": "hermes_plus_heuristic",
            "heuristic": heuristic.metadata,
            "hermes": hermes.metadata,
            "candidate_count": sum(len(items) for items in merged.values()),
        },
    )


def _heuristic_extraction_result(events: list[dict[str, Any]]) -> ExtractionResult:
    candidates_by_event_id = {
        _event_id(event): candidates
        for event in events
        if _event_id(event)
        if (candidates := heuristic_candidate_extractor(event))
    }
    return ExtractionResult(
        candidates_by_event_id=candidates_by_event_id,
        metadata={
            "extractor": "heuristic_candidate_extractor",
            "event_count": len(events),
            "candidate_count": sum(len(items) for items in candidates_by_event_id.values()),
        },
    )


def combined_extractor(result: ExtractionResult):
    """Return an event extractor combining structured metadata and extracted candidates."""

    def _extract(event: dict[str, Any]) -> list[ObserverCandidate]:
        if _is_synthetic_or_test_event(event):
            return []
        candidates = list(structured_candidate_extractor(event))
        event_id = _event_id(event)
        if event_id:
            candidates.extend(result.candidates_by_event_id.get(event_id, []))
        return candidates

    return _extract
