"""Durable Honcho contract for OMI correction-derived facts.

OMI owns correction evidence, proposals, audit trails, and rollback pointers.
Hermes/Honcho owns durable facts. This module defines the boundary between the
two systems without adding an OMI fact/entity store.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import httpx
from pydantic import BaseModel, Field

from ella.services import proposal_ingest
from ella.services.hermes_session import canonical_omi_session_key, safe_session_component

HONCHO_FACT_TOOL_NAME = "omi_correction_honcho_fact_propose"
HONCHO_FACT_WRITE_TOOL_NAME = "omi_correction_honcho_fact_write"
HONCHO_FACT_PROPOSALS_ENABLED = os.getenv("ELLA_CORRECTION_HONCHO_FACT_PROPOSALS_ENABLED", "true").lower() not in {
    "0",
    "false",
    "no",
}
HONCHO_FACT_WRITE_ENABLED = os.getenv("ELLA_CORRECTION_HONCHO_FACT_WRITE_ENABLED", "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
HONCHO_FACT_MIN_CONFIDENCE = float(os.getenv("ELLA_CORRECTION_HONCHO_FACT_MIN_CONFIDENCE", "0.9"))
HERMES_GATEWAY_URL = os.getenv("HERMES_GATEWAY_URL", "http://100.76.138.56:8642").rstrip("/")
HERMES_MODEL = os.getenv("HERMES_MODEL", "plato-eval")
HERMES_TIMEOUT_SECONDS = float(os.getenv("ELLA_CORRECTION_HONCHO_FACT_TIMEOUT_SECONDS", "20"))

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}", re.I)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
PERSON_MARKERS = re.compile(
    r"(?i)(?:is|was|named|called|a\\.k\\.a\\.|aka|person is|speaker is)\\s+([A-Z][A-Za-z0-9 .'-]{1,80})"
)
PLACE_MARKERS = re.compile(r"(?i)(?:at|in|near|from|place is|location is)\\s+([A-Z][A-Za-z0-9 .'-]{1,80})")


class HonchoFactCandidate(BaseModel):
    uid: str
    correction_id: str
    trace_id: str
    source_conversation_id: str
    related_conversation_id: str = ""
    correction_type: str
    fact_text: str
    fact_type: str = "correction_fact"
    entity_type: str = ""
    entity_name: str = ""
    person: str = ""
    place: str = ""
    environment: str = ""
    confidence: float
    fingerprint: str
    idempotency_key: str
    active_summary_version_id: str = ""
    rollback_ref: dict[str, Any] = Field(default_factory=dict)
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    session_key: str
    write_policy: str = "proposal_first"
    durable_owner: str = "honcho/hermes"


class HonchoFactWriteDecision(BaseModel):
    action: str
    reason: str = ""
    uid: str
    correction_id: str
    fingerprint: str
    idempotency_key: str
    confidence: float = 0.0
    session_key: str = ""
    status_code: int = 0
    latency_ms: int = 0
    response_ref: dict[str, Any] = Field(default_factory=dict)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stable_json(value: Any) -> str:
    return json.dumps(value or {}, sort_keys=True, separators=(",", ":"), default=str)


def _stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()[:20]


def honcho_session_key(uid: str) -> str:
    return canonical_omi_session_key(uid)


def _conversation_id(conversation: dict[str, Any]) -> str:
    return str(conversation.get("id") or conversation.get("conversation_id") or "")


def _conversation_uid(conversation: dict[str, Any], fallback_uid: str) -> str:
    return str(conversation.get("uid") or conversation.get("profile_uid") or fallback_uid)


def _summary_text(conversation: dict[str, Any]) -> str:
    structured = conversation.get("structured") if isinstance(conversation.get("structured"), dict) else {}
    return " ".join(
        str(value or "")
        for value in (
            conversation.get("title"),
            conversation.get("overview"),
            structured.get("title"),
            structured.get("overview"),
            structured.get("category"),
            conversation.get("category"),
        )
    )[:2500]


def _active_summary_version(conversation: dict[str, Any]) -> str:
    return str(conversation.get("active_summary_version_id") or "")


def _is_active_version_current(conversation: dict[str, Any], expected_active_summary_version_id: str) -> bool:
    expected = str(expected_active_summary_version_id or "")
    if not expected:
        return True
    return _active_summary_version(conversation) == expected


def _clean_fact_text(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "").replace("\x00", " ")).strip()[:1200]


def _entity_fields(correction_text: str, correction_type: str) -> dict[str, str]:
    text = _clean_fact_text(correction_text)
    fields = {"entity_type": "", "entity_name": "", "person": "", "place": "", "environment": ""}
    person_match = PERSON_MARKERS.search(text)
    place_match = PLACE_MARKERS.search(text)
    if correction_type == "identity" or person_match:
        person = (person_match.group(1) if person_match else "").strip(" .'-")
        fields.update({"entity_type": "person", "entity_name": person, "person": person})
    elif correction_type in {"media", "topic", "title"}:
        fields.update({"entity_type": correction_type, "entity_name": ""})
    if place_match:
        fields.update({"place": place_match.group(1).strip(" .'-")})
    if any(word in text.lower() for word in ("glasses", "keys", "backpack", "charger", "wallet")):
        fields["environment"] = "item_location"
    return fields


def build_honcho_fact_candidate(
    *,
    uid: str,
    correction_id: str,
    trace_id: str,
    correction_text: str,
    correction_type: str,
    source_conversation: dict[str, Any],
    related_conversation: dict[str, Any] | None = None,
    confidence: float,
) -> HonchoFactCandidate | None:
    if confidence < HONCHO_FACT_MIN_CONFIDENCE:
        return None
    if correction_type not in {"identity", "media", "topic", "title", "other"}:
        return None
    if _conversation_uid(source_conversation, uid) != uid:
        return None
    related = related_conversation or source_conversation
    if _conversation_uid(related, uid) != uid:
        return None

    fact_text = _clean_fact_text(correction_text)
    if not fact_text:
        return None
    entity = _entity_fields(fact_text, correction_type)
    source_id = _conversation_id(source_conversation)
    related_id = _conversation_id(related)
    active_summary_version_id = _active_summary_version(related)
    fingerprint_payload = {
        "uid": uid,
        "correction_type": correction_type,
        "fact_text": fact_text.lower(),
        "entity": entity,
    }
    fingerprint = _stable_hash(fingerprint_payload)
    idempotency_key = f"omi-correction-honcho-fact:{uid}:{correction_id}:{fingerprint}"
    return HonchoFactCandidate(
        uid=uid,
        correction_id=correction_id,
        trace_id=trace_id,
        source_conversation_id=source_id,
        related_conversation_id=related_id,
        correction_type=correction_type,
        fact_text=fact_text,
        confidence=round(float(confidence), 3),
        fingerprint=fingerprint,
        idempotency_key=idempotency_key,
        active_summary_version_id=active_summary_version_id,
        rollback_ref={
            "conversation_id": related_id,
            "active_summary_version_id": active_summary_version_id,
            "summary_before": _summary_text(related),
        },
        evidence=[
            {
                "kind": "source_correction",
                "conversation_id": source_id,
                "correction_id": correction_id,
                "trace_id": trace_id,
                "correction_text": fact_text,
                "correction_type": correction_type,
            },
            {
                "kind": "related_conversation_summary",
                "conversation_id": related_id,
                "content": _summary_text(related),
            },
        ],
        session_key=honcho_session_key(uid),
        **entity,
    )


def _claims(candidate: HonchoFactCandidate) -> dict[str, Any]:
    return {
        "sub": "ella-correction-observer",
        "profile_uid": candidate.uid,
        "role": "system_observer",
        "external_provider": "omi_backend",
        "grant_id": "omi-correction-honcho-fact",
        "trace_id": candidate.trace_id,
        "scopes": ["proposals:write"],
        "allowed_tools": [HONCHO_FACT_TOOL_NAME],
    }


def _proposal_payload(candidate: HonchoFactCandidate) -> dict[str, Any]:
    return {
        "title": f"Honcho fact candidate from correction {candidate.correction_id}",
        "description": candidate.fact_text,
        "target": {
            "kind": "honcho_fact_candidate",
            "durable_owner": candidate.durable_owner,
            "session_key": candidate.session_key,
            "fingerprint": candidate.fingerprint,
            "active_summary_version_id": candidate.active_summary_version_id,
        },
        "requested_change": {
            "fact_text": candidate.fact_text,
            "fact_type": candidate.fact_type,
            "correction_type": candidate.correction_type,
            "entity_type": candidate.entity_type,
            "entity_name": candidate.entity_name,
            "person": candidate.person,
            "place": candidate.place,
            "environment": candidate.environment,
            "confidence": candidate.confidence,
            "write_policy": candidate.write_policy,
            "durable_write_enabled": HONCHO_FACT_WRITE_ENABLED,
        },
        "evidence": candidate.evidence,
        "rollback_ref": candidate.rollback_ref,
        "source": "omi_correction_observer",
        "write_policy": "proposal_only" if not HONCHO_FACT_WRITE_ENABLED else "proposal_then_honcho_optional",
        "confidence": candidate.confidence,
        "durable_owner": candidate.durable_owner,
    }


def create_honcho_fact_candidate_proposal(
    candidate: HonchoFactCandidate,
    *,
    create_proposal: Callable[..., dict[str, Any]] = proposal_ingest.create_proposal,
) -> dict[str, Any]:
    if not HONCHO_FACT_PROPOSALS_ENABLED:
        return {"created": False, "skipped": True, "reason": "honcho_fact_proposals_disabled", "proposal": {}}
    return create_proposal(
        session_claims=_claims(candidate),
        tool_name=HONCHO_FACT_TOOL_NAME,
        proposal_type="memory_note",
        payload=_proposal_payload(candidate),
        idempotency_key=candidate.idempotency_key,
    )


def _write_prompt(candidate: HonchoFactCandidate) -> str:
    return (
        "Persist the following correction-derived durable fact only if it is appropriate for the "
        "active Ella/Honcho memory policy. Return compact JSON with keys: status, memory_id, reason. "
        "Do not modify OMI summaries, raw transcripts, caregiver routing, scanner rules, or reminders.\n\n"
        f"Fact candidate:\n{candidate.model_dump_json(indent=2)}"
    )


def _chat_completion_content(body: dict[str, Any]) -> str:
    choices = body.get("choices") if isinstance(body.get("choices"), list) else []
    if not choices:
        return ""
    message = choices[0].get("message") if isinstance(choices[0], dict) else {}
    return str((message or {}).get("content") or "")


def _extract_json_object(content: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        match = JSON_OBJECT_RE.search(content)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return parsed if isinstance(parsed, dict) else None


def _confirmation_from_response(response: Any) -> tuple[str, str, dict[str, Any]]:
    try:
        body = response.json()
    except Exception as exc:
        return (
            "uncertain",
            "malformed_hermes_response",
            {"error": str(exc)[:240], "body": getattr(response, "text", "")[:500]},
        )
    if not isinstance(body, dict):
        return "uncertain", "malformed_hermes_response", {"body_type": type(body).__name__}

    content = _chat_completion_content(body)
    parsed = _extract_json_object(content)
    if parsed is None:
        return "uncertain", "malformed_confirmation_json", {"content": content[:500]}

    status_value = str(parsed.get("status") or parsed.get("action") or "").strip().lower()
    durable_ref = (
        parsed.get("memory_id")
        or parsed.get("fact_id")
        or parsed.get("durable_ref")
        or parsed.get("durable_id")
        or parsed.get("id")
    )
    refused = status_value in {"refused", "rejected", "declined", "denied"} or bool(parsed.get("refusal"))
    if refused:
        return (
            "uncertain",
            "honcho_write_refused",
            {"status": status_value, "reason": str(parsed.get("reason") or "")[:500], "confirmation": parsed},
        )
    if status_value in {"error", "failed", "failure"}:
        return (
            "error",
            "honcho_write_failed",
            {"status": status_value, "reason": str(parsed.get("reason") or "")[:500], "confirmation": parsed},
        )
    if status_value in {"written", "persisted", "applied", "saved", "success", "ok"} and durable_ref:
        memory_id = str(parsed.get("memory_id") or durable_ref)
        return (
            "written",
            "hermes_honcho_write_confirmed",
            {
                "status": status_value,
                "memory_id": memory_id,
                "durable_ref": str(durable_ref),
                "confirmation": parsed,
            },
        )
    if not durable_ref:
        return (
            "uncertain",
            "missing_durable_ref",
            {"status": status_value, "reason": str(parsed.get("reason") or "")[:500], "confirmation": parsed},
        )
    return (
        "uncertain",
        "unconfirmed_honcho_write_status",
        {"status": status_value, "durable_ref": str(durable_ref), "confirmation": parsed},
    )


async def write_honcho_fact_candidate(
    candidate: HonchoFactCandidate,
    *,
    current_conversation: dict[str, Any] | None = None,
    token: str | None = None,
    gateway_url: str | None = None,
    model: str | None = None,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> HonchoFactWriteDecision:
    if not HONCHO_FACT_WRITE_ENABLED:
        return HonchoFactWriteDecision(
            action="skip",
            reason="durable_write_disabled",
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
        )
    if current_conversation is not None and _conversation_uid(current_conversation, candidate.uid) != candidate.uid:
        return HonchoFactWriteDecision(
            action="skip",
            reason="cross_user_current_conversation",
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
        )
    if current_conversation is not None and not _is_active_version_current(
        current_conversation, candidate.active_summary_version_id
    ):
        return HonchoFactWriteDecision(
            action="skip",
            reason="stale_active_summary_version",
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
        )

    api_token = token if token is not None else os.getenv("HERMES_API_SERVER_KEY", os.getenv("API_SERVER_KEY", ""))
    if not api_token:
        return HonchoFactWriteDecision(
            action="skip",
            reason="missing_hermes_token",
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
        )

    url = (gateway_url or HERMES_GATEWAY_URL).rstrip("/")
    started = time.monotonic()
    session_id = (
        f"honcho-fact:{safe_session_component(candidate.uid)}:{candidate.correction_id}:{candidate.fingerprint}"
    )
    try:
        async with http_client_factory(timeout=HERMES_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{url}/v1/chat/completions",
                headers={
                    "Authorization": f"Bearer {api_token}",
                    "Content-Type": "application/json",
                    "X-Hermes-Session-Id": session_id,
                    "X-Hermes-Session-Key": candidate.session_key,
                    "X-Idempotency-Key": candidate.idempotency_key,
                    "X-Trace-Id": candidate.trace_id,
                },
                json={
                    "model": model or HERMES_MODEL,
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "You are the Hermes/Honcho durable memory writer for Ella. "
                                "Use the supplied session key as the durable memory scope."
                            ),
                        },
                        {"role": "user", "content": _write_prompt(candidate)},
                    ],
                    "temperature": 0,
                    "max_tokens": 300,
                    "stream": False,
                },
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return HonchoFactWriteDecision(
                action="error",
                reason=f"hermes_http_{response.status_code}",
                uid=candidate.uid,
                correction_id=candidate.correction_id,
                fingerprint=candidate.fingerprint,
                idempotency_key=candidate.idempotency_key,
                confidence=candidate.confidence,
                session_key=candidate.session_key,
                status_code=response.status_code,
                latency_ms=latency_ms,
                response_ref={"body": getattr(response, "text", "")[:500]},
            )
        action, reason, response_ref = _confirmation_from_response(response)
        response_ref = {"transport": "hermes_chat_completions", "at": _now_iso(), **response_ref}
        return HonchoFactWriteDecision(
            action=action,
            reason=reason,
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
            status_code=response.status_code,
            latency_ms=latency_ms,
            response_ref=response_ref,
        )
    except Exception as exc:
        return HonchoFactWriteDecision(
            action="error",
            reason=type(exc).__name__,
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
            latency_ms=int((time.monotonic() - started) * 1000),
            response_ref={"error": str(exc)[:500]},
        )
