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
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import httpx
from pydantic import BaseModel, Field

from database.honcho_attestation import authority_credential
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
HONCHO_FACT_WRITE_TRANSPORT = os.getenv("ELLA_CORRECTION_HONCHO_FACT_WRITE_TRANSPORT", "hermes").strip().lower()
HONCHO_BASE_URL = (
    os.getenv("ELLA_CORRECTION_HONCHO_BASE_URL") or os.getenv("HONCHO_BASE_URL") or "http://127.0.0.1:8320"
).rstrip("/")
# This legacy chain is truthiness-based; unlike presence-sensitive pairs, an
# explicitly empty correction key intentionally permits the shared fallback.
HONCHO_API_KEY = authority_credential("ELLA_CORRECTION_HONCHO_API_KEY", strip=False) or authority_credential(
    "HONCHO_API_KEY", strip=False
)
HONCHO_WORKSPACE = os.getenv("ELLA_CORRECTION_HONCHO_WORKSPACE") or os.getenv("HONCHO_WORKSPACE") or ""
HONCHO_WORKSPACE_PREFIX = os.getenv("ELLA_CORRECTION_HONCHO_WORKSPACE_PREFIX", "ella-correction-facts")
HONCHO_OBSERVER_PEER_ID = (
    os.getenv("ELLA_CORRECTION_HONCHO_OBSERVER_PEER_ID") or os.getenv("HONCHO_OBSERVER_PEER_ID") or ""
)
HONCHO_OBSERVED_PEER_ID = (
    os.getenv("ELLA_CORRECTION_HONCHO_OBSERVED_PEER_ID") or os.getenv("HONCHO_OBSERVED_PEER_ID") or ""
)
HONCHO_PROFILE_MAP_JSON = os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_MAP_JSON", "")
HONCHO_PROFILE_MAP_PATH = os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_MAP_PATH", "")
HONCHO_PROFILE_MAP_URL = os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_MAP_URL", "")
HONCHO_PROFILE_MAP_URL_TOKEN = authority_credential("ELLA_CORRECTION_HONCHO_PROFILE_MAP_TOKEN", strip=False)
HONCHO_PROFILE_MAP_URL_TTL_SECONDS = float(os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_MAP_URL_TTL_SECONDS", "30"))
HONCHO_PROFILE_CONFIG_PATH = os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_CONFIG_PATH", "")
HONCHO_PROFILE_UID = os.getenv("ELLA_CORRECTION_HONCHO_PROFILE_UID", "")

TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9'_-]{2,}", re.I)
JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)
HONCHO_RESOURCE_RE = re.compile(r"[^a-zA-Z0-9_-]+")
_PROFILE_MAP_URL_CACHE: dict[str, Any] = {"url": "", "fetched_at": 0.0, "data": None}
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


def _honcho_resource_id(value: str, *, prefix: str = "", max_len: int = 100) -> str:
    raw = f"{prefix}{value}".strip()
    sanitized = HONCHO_RESOURCE_RE.sub("-", raw).strip("-")
    if not sanitized:
        sanitized = "unknown"
    if len(sanitized) <= max_len and sanitized == raw:
        return sanitized
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:10]
    room = max_len - len(digest) - 1
    return f"{sanitized[:room].rstrip('-')}-{digest}"


def _honcho_workspace_id(candidate: HonchoFactCandidate, explicit_workspace: str | None = None) -> str:
    if explicit_workspace:
        return _honcho_resource_id(explicit_workspace)
    if HONCHO_WORKSPACE:
        return _honcho_resource_id(HONCHO_WORKSPACE)
    uid_hash = hashlib.sha256(candidate.uid.encode("utf-8")).hexdigest()[:12]
    uid_component = _honcho_resource_id(candidate.uid, max_len=48)
    return _honcho_resource_id(f"{HONCHO_WORKSPACE_PREFIX}-{uid_component}-{uid_hash}")


def _safe_json_loads(value: str) -> Any:
    if not str(value or "").strip():
        return None
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return None


def _safe_json_file(path: str) -> Any:
    if not str(path or "").strip():
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            return json.load(handle)
    except (OSError, json.JSONDecodeError):
        return None


def _safe_json_url(url: str, *, timeout_seconds: float = 5.0) -> Any:
    if not str(url or "").strip():
        return None
    now = time.monotonic()
    if (
        _PROFILE_MAP_URL_CACHE.get("url") == url
        and _PROFILE_MAP_URL_CACHE.get("data") is not None
        and now - float(_PROFILE_MAP_URL_CACHE.get("fetched_at") or 0) < HONCHO_PROFILE_MAP_URL_TTL_SECONDS
    ):
        return _PROFILE_MAP_URL_CACHE["data"]
    headers = {"Accept": "application/json"}
    if HONCHO_PROFILE_MAP_URL_TOKEN:
        headers["Authorization"] = f"Bearer {HONCHO_PROFILE_MAP_URL_TOKEN}"
    try:
        with urlopen(
            Request(url, headers=headers),
            timeout=max(0.01, float(timeout_seconds)),
        ) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, OSError, json.JSONDecodeError, TimeoutError):
        return None
    if isinstance(data, dict) and isinstance(data.get("entries"), dict):
        data = data["entries"]
    _PROFILE_MAP_URL_CACHE.update({"url": url, "fetched_at": now, "data": data})
    return data


def _profile_target_from_entry(entry: Any) -> dict[str, str] | None:
    if not isinstance(entry, dict):
        return None
    hosts = entry.get("hosts") if isinstance(entry.get("hosts"), dict) else {}
    host_entry = hosts.get(f"hermes.{HERMES_MODEL}") if isinstance(hosts.get(f"hermes.{HERMES_MODEL}"), dict) else {}
    if host_entry:
        entry = {**entry, **host_entry}
    workspace = str(entry.get("workspace") or entry.get("honcho_workspace") or "").strip()
    observed_peer_id = str(
        entry.get("observed_peer_id")
        or entry.get("observedPeerId")
        or entry.get("peerName")
        or entry.get("peer_name")
        or ""
    ).strip()
    if not workspace or not observed_peer_id:
        return None
    observer_peer_id = str(
        entry.get("observer_peer_id")
        or entry.get("observerPeerId")
        or entry.get("aiPeer")
        or entry.get("ai_peer")
        or ""
    ).strip()
    return {
        "workspace": _honcho_resource_id(workspace),
        "observed_peer_id": _honcho_resource_id(observed_peer_id),
        "observer_peer_id": _honcho_resource_id(observer_peer_id) if observer_peer_id else "",
        "source": str(entry.get("source") or "profile_mapping"),
    }


def _target_from_profile_map_data(uid: str, data: Any) -> dict[str, str] | None:
    if isinstance(data, dict):
        if uid in data:
            target = _profile_target_from_entry(data[uid])
            if target:
                return {**target, "source": "profile_map"}
        profiles = data.get("profiles") or data.get("users")
        if isinstance(profiles, list):
            data = profiles
    if isinstance(data, list):
        for entry in data:
            if not isinstance(entry, dict):
                continue
            entry_uid = str(entry.get("uid") or entry.get("profile_uid") or entry.get("profileUid") or "").strip()
            if entry_uid != uid:
                continue
            target = _profile_target_from_entry(entry)
            if target:
                return {**target, "source": "profile_map"}
    return None


def _target_from_local_profile_map(uid: str) -> dict[str, str] | None:
    uid = str(uid or "").strip()
    for data in (
        _safe_json_loads(HONCHO_PROFILE_MAP_JSON),
        _safe_json_file(HONCHO_PROFILE_MAP_PATH),
    ):
        target = _target_from_profile_map_data(uid, data)
        if target:
            return target
    return None


def _target_from_remote_profile_map(
    uid: str,
    *,
    timeout_seconds: float = 5.0,
) -> dict[str, str] | None:
    return _target_from_profile_map_data(
        str(uid or "").strip(),
        _safe_json_url(
            HONCHO_PROFILE_MAP_URL,
            timeout_seconds=timeout_seconds,
        ),
    )


def _target_from_profile_config(uid: str) -> dict[str, str] | None:
    uid = str(uid or "").strip()
    if not HONCHO_PROFILE_UID or HONCHO_PROFILE_UID.strip() != uid:
        return None
    data = _safe_json_file(HONCHO_PROFILE_CONFIG_PATH)
    target = _profile_target_from_entry(data)
    if target:
        return {**target, "source": "profile_config"}
    return None


def resolve_companion_honcho_target(
    uid: str,
    *,
    remote_timeout_seconds: float = 5.0,
) -> tuple[dict[str, str] | None, str]:
    """Resolve an exact user profile without correction-write global fallbacks."""
    uid = str(uid or "").strip()
    if not uid:
        return None, "missing_companion_honcho_target"
    # Every local exact-UID source must win before remote provisioning I/O.
    target = (
        _target_from_local_profile_map(uid)
        or _target_from_profile_config(uid)
        or _target_from_remote_profile_map(
            uid,
            timeout_seconds=remote_timeout_seconds,
        )
    )
    if not target:
        return None, "missing_companion_honcho_target"
    return (
        {
            "workspace": target["workspace"],
            "observer_peer_id": _honcho_resource_id(target.get("observer_peer_id") or "ella-correction-observer"),
            "observed_peer_id": target["observed_peer_id"],
            "source": target.get("source") or "companion_profile",
        },
        "",
    )


def _resolve_native_honcho_target(
    candidate: HonchoFactCandidate,
    *,
    honcho_workspace: str | None = None,
    honcho_observer_peer_id: str | None = None,
    honcho_observed_peer_id: str | None = None,
) -> tuple[dict[str, str] | None, str]:
    explicit_workspace = honcho_workspace if honcho_workspace is not None else HONCHO_WORKSPACE
    explicit_observed = honcho_observed_peer_id if honcho_observed_peer_id is not None else HONCHO_OBSERVED_PEER_ID
    explicit_observer = honcho_observer_peer_id if honcho_observer_peer_id is not None else HONCHO_OBSERVER_PEER_ID

    if explicit_workspace:
        return (
            {
                "workspace": _honcho_workspace_id(candidate, explicit_workspace),
                "observer_peer_id": _honcho_resource_id(explicit_observer or "ella-correction-observer"),
                "observed_peer_id": _honcho_resource_id(
                    explicit_observed or candidate.uid, prefix="" if explicit_observed else "omi-uid-"
                ),
                "source": "explicit_override",
            },
            "",
        )

    target, _ = resolve_companion_honcho_target(candidate.uid)
    if target:
        observer_peer_id = explicit_observer or target["observer_peer_id"]
        return (
            {
                "workspace": target["workspace"],
                "observer_peer_id": _honcho_resource_id(observer_peer_id),
                "observed_peer_id": target["observed_peer_id"],
                "source": target.get("source") or "companion_profile",
            },
            "",
        )

    return None, "missing_companion_honcho_target"


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


def _honcho_headers(api_key: str) -> dict[str, str]:
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _honcho_success_ref(body: Any) -> dict[str, Any] | None:
    if not isinstance(body, list) or not body or not isinstance(body[0], dict):
        return None
    first = body[0]
    durable_ref = first.get("id") or first.get("memory_id") or first.get("durable_ref")
    if not durable_ref:
        return None
    return {
        "memory_id": str(durable_ref),
        "durable_ref": str(durable_ref),
        "honcho_conclusion": {
            key: first.get(key)
            for key in ("id", "observer_id", "observer", "observed_id", "observed", "session_id", "session_name")
            if first.get(key) is not None
        },
    }


def _honcho_existing_ref(body: Any, *, fact_text: str, session_id: str) -> dict[str, Any] | None:
    if not isinstance(body, dict) or not isinstance(body.get("items"), list):
        return None
    for item in body["items"]:
        if not isinstance(item, dict):
            continue
        if str(item.get("content") or "").strip() != fact_text.strip():
            continue
        item_session_id = str(item.get("session_id") or item.get("session_name") or "")
        if item_session_id and item_session_id != session_id:
            continue
        durable_ref = item.get("id") or item.get("memory_id") or item.get("durable_ref")
        if durable_ref:
            return {
                "memory_id": str(durable_ref),
                "durable_ref": str(durable_ref),
                "honcho_conclusion": {
                    key: item.get(key)
                    for key in (
                        "id",
                        "observer_id",
                        "observer",
                        "observed_id",
                        "observed",
                        "session_id",
                        "session_name",
                    )
                    if item.get(key) is not None
                },
            }
    return None


async def _write_honcho_fact_candidate_via_native_honcho(
    candidate: HonchoFactCandidate,
    *,
    honcho_base_url: str | None = None,
    honcho_api_key: str | None = None,
    honcho_workspace: str | None = None,
    honcho_observer_peer_id: str | None = None,
    honcho_observed_peer_id: str | None = None,
    http_client_factory: Callable[..., Any] = httpx.AsyncClient,
) -> HonchoFactWriteDecision:
    api_key = honcho_api_key if honcho_api_key is not None else HONCHO_API_KEY
    target, target_error = _resolve_native_honcho_target(
        candidate,
        honcho_workspace=honcho_workspace,
        honcho_observer_peer_id=honcho_observer_peer_id,
        honcho_observed_peer_id=honcho_observed_peer_id,
    )
    if target is None:
        return HonchoFactWriteDecision(
            action="skip",
            reason=target_error,
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
            response_ref={"transport": "honcho_conclusions"},
        )
    workspace = target["workspace"]
    observer_peer_id = target["observer_peer_id"]
    observed_peer_id = target["observed_peer_id"]
    session_id = _honcho_resource_id(candidate.session_key)
    base_url = (honcho_base_url or HONCHO_BASE_URL).rstrip("/")
    started = time.monotonic()

    try:
        async with http_client_factory(timeout=HERMES_TIMEOUT_SECONDS) as client:
            headers = _honcho_headers(api_key)
            setup_calls = [
                (
                    f"{base_url}/v3/workspaces",
                    {"id": workspace, "metadata": {"source": "omi_correction_observer"}},
                ),
                (
                    f"{base_url}/v3/workspaces/{workspace}/peers",
                    {"id": observer_peer_id, "metadata": {"role": "observer", "source": "omi_correction_observer"}},
                ),
                (
                    f"{base_url}/v3/workspaces/{workspace}/peers",
                    {"id": observed_peer_id, "metadata": {"profile_uid": candidate.uid, "source": "omi_correction"}},
                ),
                (
                    f"{base_url}/v3/workspaces/{workspace}/sessions",
                    {
                        "id": session_id,
                        "metadata": {
                            "profile_uid": candidate.uid,
                            "source": "omi_correction_observer",
                            "session_key": candidate.session_key,
                        },
                        "peers": {
                            observer_peer_id: {"observe_me": True, "observe_others": True},
                            observed_peer_id: {"observe_me": True, "observe_others": True},
                        },
                    },
                ),
            ]
            for setup_url, payload in setup_calls:
                response = await client.post(setup_url, headers=headers, json=payload)
                if response.status_code >= 400:
                    return HonchoFactWriteDecision(
                        action="error",
                        reason=f"honcho_setup_http_{response.status_code}",
                        uid=candidate.uid,
                        correction_id=candidate.correction_id,
                        fingerprint=candidate.fingerprint,
                        idempotency_key=candidate.idempotency_key,
                        confidence=candidate.confidence,
                        session_key=candidate.session_key,
                        status_code=response.status_code,
                        latency_ms=int((time.monotonic() - started) * 1000),
                        response_ref={
                            "transport": "honcho_conclusions",
                            "workspace": workspace,
                            "target_source": target.get("source"),
                            "body": getattr(response, "text", "")[:500],
                        },
                    )

            idempotency_response = await client.post(
                f"{base_url}/v3/workspaces/{workspace}/conclusions/list",
                headers=headers,
                json={"filters": {"observer_id": observer_peer_id, "observed_id": observed_peer_id}},
            )
            if idempotency_response.status_code >= 400:
                return HonchoFactWriteDecision(
                    action="error",
                    reason=f"honcho_idempotency_http_{idempotency_response.status_code}",
                    uid=candidate.uid,
                    correction_id=candidate.correction_id,
                    fingerprint=candidate.fingerprint,
                    idempotency_key=candidate.idempotency_key,
                    confidence=candidate.confidence,
                    session_key=candidate.session_key,
                    status_code=idempotency_response.status_code,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    response_ref={
                        "transport": "honcho_conclusions",
                        "workspace": workspace,
                        "target_source": target.get("source"),
                        "body": getattr(idempotency_response, "text", "")[:500],
                    },
                )
            try:
                existing_body = idempotency_response.json()
            except Exception as exc:
                return HonchoFactWriteDecision(
                    action="uncertain",
                    reason="malformed_honcho_idempotency_response",
                    uid=candidate.uid,
                    correction_id=candidate.correction_id,
                    fingerprint=candidate.fingerprint,
                    idempotency_key=candidate.idempotency_key,
                    confidence=candidate.confidence,
                    session_key=candidate.session_key,
                    status_code=idempotency_response.status_code,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    response_ref={"transport": "honcho_conclusions", "workspace": workspace, "error": str(exc)[:240]},
                )
            existing_ref = _honcho_existing_ref(existing_body, fact_text=candidate.fact_text, session_id=session_id)
            if existing_ref:
                return HonchoFactWriteDecision(
                    action="written",
                    reason="honcho_conclusion_already_exists",
                    uid=candidate.uid,
                    correction_id=candidate.correction_id,
                    fingerprint=candidate.fingerprint,
                    idempotency_key=candidate.idempotency_key,
                    confidence=candidate.confidence,
                    session_key=candidate.session_key,
                    status_code=idempotency_response.status_code,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    response_ref={
                        "transport": "honcho_conclusions",
                        "workspace": workspace,
                        "target_source": target.get("source"),
                        "observer_peer_id": observer_peer_id,
                        "observed_peer_id": observed_peer_id,
                        "session_id": session_id,
                        "idempotency": "backend_exact_match",
                        "at": _now_iso(),
                        **existing_ref,
                    },
                )

            response = await client.post(
                f"{base_url}/v3/workspaces/{workspace}/conclusions",
                headers={**headers, "X-Idempotency-Key": candidate.idempotency_key, "X-Trace-Id": candidate.trace_id},
                json={
                    "conclusions": [
                        {
                            "observer_id": observer_peer_id,
                            "observed_id": observed_peer_id,
                            "content": candidate.fact_text,
                            "session_id": session_id,
                        }
                    ]
                },
            )
        latency_ms = int((time.monotonic() - started) * 1000)
        if response.status_code >= 400:
            return HonchoFactWriteDecision(
                action="error",
                reason=f"honcho_http_{response.status_code}",
                uid=candidate.uid,
                correction_id=candidate.correction_id,
                fingerprint=candidate.fingerprint,
                idempotency_key=candidate.idempotency_key,
                confidence=candidate.confidence,
                session_key=candidate.session_key,
                status_code=response.status_code,
                latency_ms=latency_ms,
                response_ref={
                    "transport": "honcho_conclusions",
                    "workspace": workspace,
                    "target_source": target.get("source"),
                    "body": getattr(response, "text", "")[:500],
                },
            )

        try:
            body = response.json()
        except Exception as exc:
            return HonchoFactWriteDecision(
                action="uncertain",
                reason="malformed_honcho_response",
                uid=candidate.uid,
                correction_id=candidate.correction_id,
                fingerprint=candidate.fingerprint,
                idempotency_key=candidate.idempotency_key,
                confidence=candidate.confidence,
                session_key=candidate.session_key,
                status_code=response.status_code,
                latency_ms=latency_ms,
                response_ref={"transport": "honcho_conclusions", "workspace": workspace, "error": str(exc)[:240]},
            )

        success_ref = _honcho_success_ref(body)
        if not success_ref:
            return HonchoFactWriteDecision(
                action="uncertain",
                reason="missing_honcho_durable_ref",
                uid=candidate.uid,
                correction_id=candidate.correction_id,
                fingerprint=candidate.fingerprint,
                idempotency_key=candidate.idempotency_key,
                confidence=candidate.confidence,
                session_key=candidate.session_key,
                status_code=response.status_code,
                latency_ms=latency_ms,
                response_ref={"transport": "honcho_conclusions", "workspace": workspace, "body": body},
            )

        return HonchoFactWriteDecision(
            action="written",
            reason="honcho_conclusion_written",
            uid=candidate.uid,
            correction_id=candidate.correction_id,
            fingerprint=candidate.fingerprint,
            idempotency_key=candidate.idempotency_key,
            confidence=candidate.confidence,
            session_key=candidate.session_key,
            status_code=response.status_code,
            latency_ms=latency_ms,
            response_ref={
                "transport": "honcho_conclusions",
                "workspace": workspace,
                "target_source": target.get("source"),
                "observer_peer_id": observer_peer_id,
                "observed_peer_id": observed_peer_id,
                "session_id": session_id,
                "at": _now_iso(),
                **success_ref,
            },
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
            response_ref={"transport": "honcho_conclusions", "error": str(exc)[:500]},
        )


async def write_honcho_fact_candidate(
    candidate: HonchoFactCandidate,
    *,
    current_conversation: dict[str, Any] | None = None,
    token: str | None = None,
    gateway_url: str | None = None,
    model: str | None = None,
    transport: str | None = None,
    honcho_base_url: str | None = None,
    honcho_api_key: str | None = None,
    honcho_workspace: str | None = None,
    honcho_observer_peer_id: str | None = None,
    honcho_observed_peer_id: str | None = None,
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

    selected_transport = (transport or HONCHO_FACT_WRITE_TRANSPORT or "hermes").strip().lower()
    if selected_transport in {"honcho", "honcho_conclusions", "native_honcho"}:
        return await _write_honcho_fact_candidate_via_native_honcho(
            candidate,
            honcho_base_url=honcho_base_url,
            honcho_api_key=honcho_api_key,
            honcho_workspace=honcho_workspace,
            honcho_observer_peer_id=honcho_observer_peer_id,
            honcho_observed_peer_id=honcho_observed_peer_id,
            http_client_factory=http_client_factory,
        )
    if selected_transport not in {"hermes", "hermes_chat", "hermes_chat_completions"}:
        return HonchoFactWriteDecision(
            action="skip",
            reason=f"unsupported_transport:{selected_transport}",
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
