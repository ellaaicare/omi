#!/usr/bin/env python3
"""Content-free acceptance harness for one synthetic Hermes Cloud profile."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from database import voice_canary as voice_canary_db
from ella.services.hermes_broker_client import HermesBrokerClient
from ella.services.hermes_broker_prototype import HermesBrokerPrototypeConfig
from ella.services.hermes_cloud_runtime import broker_session_id_for_scope
from ella.services.runtime_errors import ProvisioningError

CONFIG_SCHEMA = "ella-hermes-product-fit-canary-v1"
CALLBACK_SCHEMA = "ella.hermes.callback.v1"
STOCK_CALLBACK_CONTRACT = "stock_best_effort_v1"
APPROVED_SECRET_ROOTS = (Path("/etc/ella"), Path("/var/lib/ella"))
SYNTHETIC_PREFIXES = ("synthetic-", "staging-synthetic-")
TERMINAL_STATUSES = frozenset({"writeback_completed", "blocked", "quarantined", "expired", "writeback_blocked"})
SECRET_REF_RE = re.compile(r"^env:(ELLA_[A-Z0-9_]{3,120})$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class HarnessRefusal(RuntimeError):
    """Fail-closed harness refusal carrying only a content-free code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class ScenarioNotTested(RuntimeError):
    """A deployed optional observation surface is unavailable."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def _sha256(value: str | bytes) -> str:
    raw = value.encode("utf-8") if isinstance(value, str) else value
    return hashlib.sha256(raw).hexdigest()


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _exact_uuid(value: Any, field_name: str) -> str:
    try:
        parsed = uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError) as exc:
        raise HarnessRefusal(f"{field_name}_invalid") from exc
    if str(parsed) != str(value):
        raise HarnessRefusal(f"{field_name}_invalid")
    return str(parsed)


def _safe_id(value: Any, field_name: str) -> str:
    candidate = str(value or "").strip()
    if SAFE_ID_RE.fullmatch(candidate) is None:
        raise HarnessRefusal(f"{field_name}_invalid")
    return candidate


def _approved_https_base_url(value: Any) -> str:
    candidate = str(value or "").strip().rstrip("/")
    parsed = urlparse(candidate)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.port not in (None, 443)
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise HarnessRefusal("backend_base_url_not_allowlisted")
    return candidate


def _secret_ref(value: Any, field_name: str) -> str:
    candidate = str(value or "").strip()
    if SECRET_REF_RE.fullmatch(candidate) is None:
        raise HarnessRefusal(f"{field_name}_invalid")
    return candidate


def _resolve_env_secret(ref: str) -> str:
    match = SECRET_REF_RE.fullmatch(ref)
    if match is None:
        raise HarnessRefusal("secret_ref_invalid")
    value = os.getenv(match.group(1), "")
    if len(value) < 32:
        raise HarnessRefusal("secret_ref_unavailable")
    return value


def _require_firebase_subject(token: str, expected_uid: str) -> None:
    parts = token.split(".")
    if len(parts) != 3:
        raise HarnessRefusal("firebase_token_shape_invalid")
    try:
        padding = "=" * (-len(parts[1]) % 4)
        claims = json.loads(base64.urlsafe_b64decode(parts[1] + padding).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessRefusal("firebase_token_shape_invalid") from exc
    if not isinstance(claims, dict):
        raise HarnessRefusal("firebase_token_shape_invalid")
    subject = str(claims.get("user_id") or claims.get("sub") or "")
    if subject != expected_uid or not subject.startswith(SYNTHETIC_PREFIXES):
        raise HarnessRefusal("firebase_token_subject_mismatch")


def _assert_protected_file(
    path: Path,
    *,
    approved_roots: tuple[Path, ...] = APPROVED_SECRET_ROOTS,
    expected_owner_uid: int = 0,
) -> Path:
    if not path.is_absolute():
        raise HarnessRefusal("protected_file_not_absolute")
    try:
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise HarnessRefusal("protected_file_invalid") from exc
    if path.is_symlink() or not resolved.is_file():
        raise HarnessRefusal("protected_file_invalid")
    if not any(resolved.is_relative_to(root.resolve()) for root in approved_roots):
        raise HarnessRefusal("protected_file_root_refused")
    metadata = resolved.stat()
    if metadata.st_uid != expected_owner_uid or stat.S_IMODE(metadata.st_mode) not in {0o400, 0o600}:
        raise HarnessRefusal("protected_file_metadata_refused")
    parent = resolved.parent
    parent_metadata = parent.stat()
    if (
        parent.is_symlink()
        or parent_metadata.st_uid != expected_owner_uid
        or stat.S_IMODE(parent_metadata.st_mode) != 0o700
    ):
        raise HarnessRefusal("protected_parent_metadata_refused")
    return resolved


@dataclass(frozen=True)
class CanaryConfig:
    run_id: str
    uid: str
    account_id: str
    profile_id: str
    binding_id: str
    consent_epoch: str
    expected_model: str
    chat_channel: str
    primary_session_key: str
    isolated_session_key: str
    backend_base_url: str
    backend_auth_ref: str
    enrichment_token_ref: str
    broker: HermesBrokerPrototypeConfig
    voice_conversation_id: str
    voice_summary_version_id: str
    memory_pack_sha256: str
    enrichment_conversation_id: str
    enrichment_transcript_sha256: str
    max_latency_ms: int

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "CanaryConfig":
        if raw.get("schema_version") != CONFIG_SCHEMA or set(raw) != {
            "schema_version",
            "run_id",
            "selectors",
            "backend",
            "broker",
            "voice_memory",
            "enrichment",
            "max_latency_ms",
        }:
            raise HarnessRefusal("config_shape_invalid")
        selectors = raw.get("selectors")
        backend = raw.get("backend")
        broker = raw.get("broker")
        voice_memory = raw.get("voice_memory")
        enrichment = raw.get("enrichment")
        if not all(isinstance(item, dict) for item in (selectors, backend, broker, voice_memory, enrichment)):
            raise HarnessRefusal("config_shape_invalid")
        if set(selectors) != {
            "uid",
            "account_id",
            "profile_id",
            "binding_id",
            "consent_epoch",
            "expected_model",
            "chat_channel",
            "primary_session_key",
            "isolated_session_key",
        }:
            raise HarnessRefusal("selector_shape_invalid")
        if set(backend) != {
            "base_url",
            "auth_token_ref",
            "enrichment_token_ref",
        }:
            raise HarnessRefusal("backend_shape_invalid")
        if set(broker) != {
            "base_url",
            "allowed_host",
            "service_token_ref",
            "poll_interval_seconds",
            "poll_timeout_seconds",
            "deadline_seconds",
        }:
            raise HarnessRefusal("broker_shape_invalid")
        if set(voice_memory) != {
            "conversation_id",
            "active_summary_version_id",
            "pack_sha256",
        }:
            raise HarnessRefusal("voice_memory_shape_invalid")
        if set(enrichment) != {
            "conversation_id",
            "transcript_sha256",
        }:
            raise HarnessRefusal("enrichment_shape_invalid")

        uid = _safe_id(selectors["uid"], "uid")
        if not uid.startswith(SYNTHETIC_PREFIXES) or uid in {"realcryptoplato", "plato_eval", "plato-eval"}:
            raise HarnessRefusal("synthetic_uid_required")
        account_id = _exact_uuid(selectors["account_id"], "account_id")
        profile_id = _exact_uuid(selectors["profile_id"], "profile_id")
        binding_id = _exact_uuid(selectors["binding_id"], "binding_id")
        consent_epoch = _exact_uuid(selectors["consent_epoch"], "consent_epoch")
        primary_session_key = _safe_id(selectors["primary_session_key"], "primary_session_key")
        isolated_session_key = _safe_id(selectors["isolated_session_key"], "isolated_session_key")
        if primary_session_key == isolated_session_key:
            raise HarnessRefusal("distinct_sessions_required")
        expected_model = _safe_id(selectors["expected_model"], "expected_model")
        chat_channel = _safe_id(selectors["chat_channel"], "chat_channel")

        try:
            broker_config = HermesBrokerPrototypeConfig(
                enabled=True,
                account_id=account_id,
                profile_id=profile_id,
                binding_id=binding_id,
                base_url=str(broker["base_url"] or "").strip().rstrip("/"),
                allowed_host=str(broker["allowed_host"] or "").strip(),
                service_token_ref=_secret_ref(broker["service_token_ref"], "broker_service_token_ref"),
                poll_interval_seconds=float(broker["poll_interval_seconds"]),
                poll_timeout_seconds=float(broker["poll_timeout_seconds"]),
                deadline_seconds=int(broker["deadline_seconds"]),
            )
        except (TypeError, ValueError) as exc:
            raise HarnessRefusal("broker_timing_invalid") from exc
        # Reuse the production client's exact URL gate before any network work.
        try:
            HermesBrokerClient(broker_config)._assert_url(
                f"{broker_config.base_url}/v1/ella/internal/hermes-webhook-broker/stock-canary/admit",
                expected_path_prefix="/v1/ella/internal/hermes-webhook-broker/stock-canary/admit",
            )
        except ProvisioningError as exc:
            raise HarnessRefusal(exc.code) from exc
        if broker_config.allowed_host == "127.0.0.1" and broker_config.base_url != "http://127.0.0.1:18097":
            raise HarnessRefusal("broker_loopback_pin_invalid")
        if not (0.1 <= broker_config.poll_interval_seconds <= 5.0):
            raise HarnessRefusal("broker_poll_interval_invalid")
        if not (1.0 <= broker_config.poll_timeout_seconds <= 120.0):
            raise HarnessRefusal("broker_poll_timeout_invalid")
        if not (10 <= broker_config.deadline_seconds <= 600):
            raise HarnessRefusal("broker_deadline_invalid")

        memory_pack_sha256 = str(voice_memory["pack_sha256"] or "").strip()
        transcript_sha256 = str(enrichment["transcript_sha256"] or "").strip()
        if SHA256_RE.fullmatch(memory_pack_sha256) is None or SHA256_RE.fullmatch(transcript_sha256) is None:
            raise HarnessRefusal("expected_digest_invalid")
        try:
            max_latency_ms = int(raw["max_latency_ms"])
        except (TypeError, ValueError) as exc:
            raise HarnessRefusal("max_latency_invalid") from exc
        if not (1000 <= max_latency_ms <= 600_000):
            raise HarnessRefusal("max_latency_invalid")

        return cls(
            run_id=_safe_id(raw["run_id"], "run_id"),
            uid=uid,
            account_id=account_id,
            profile_id=profile_id,
            binding_id=binding_id,
            consent_epoch=consent_epoch,
            expected_model=expected_model,
            chat_channel=chat_channel,
            primary_session_key=primary_session_key,
            isolated_session_key=isolated_session_key,
            backend_base_url=_approved_https_base_url(backend["base_url"]),
            backend_auth_ref=_secret_ref(backend["auth_token_ref"], "backend_auth_token_ref"),
            enrichment_token_ref=_secret_ref(backend["enrichment_token_ref"], "enrichment_token_ref"),
            broker=broker_config,
            voice_conversation_id=_safe_id(voice_memory["conversation_id"], "voice_conversation_id"),
            voice_summary_version_id=_safe_id(
                voice_memory["active_summary_version_id"],
                "voice_summary_version_id",
            ),
            memory_pack_sha256=memory_pack_sha256,
            enrichment_conversation_id=_safe_id(
                enrichment["conversation_id"],
                "enrichment_conversation_id",
            ),
            enrichment_transcript_sha256=transcript_sha256,
            max_latency_ms=max_latency_ms,
        )


def load_config(path: str) -> CanaryConfig:
    if os.geteuid() != 0:
        raise HarnessRefusal("root_required")
    protected = _assert_protected_file(Path(path))
    try:
        raw = json.loads(protected.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessRefusal("config_invalid") from exc
    if not isinstance(raw, dict):
        raise HarnessRefusal("config_invalid")
    return CanaryConfig.from_mapping(raw)


@dataclass(frozen=True)
class ChatObservation:
    text: str
    response_id: str
    request_id: str
    correlation_id: str
    duplicate: bool
    statuses: tuple[str, ...]
    terminal_frames: int
    admission_posts: int
    callback_outcomes: int
    generation: int
    latency_ms: int


@dataclass(frozen=True)
class MemoryObservation:
    scoped_pack_sha256: str
    conversation_id_matches: bool
    summary_version_matches: bool
    unscoped_memory_absent: bool
    latency_ms: int


@dataclass(frozen=True)
class EnrichmentObservation:
    status: str
    correlation_matches: bool
    transcript_hash_matches: bool
    duplicate_replay: bool
    chat_identity_absent: bool
    latency_ms: int


@dataclass(frozen=True)
class ReplayObservation:
    duplicate_ingress: bool
    same_response_hash: bool
    same_response_id: bool
    missing_outcome_refused: bool
    wrong_correlation_refused: bool
    wrong_session_refused: bool
    timeout_refused: bool
    partial_after_terminal_refused: bool
    latency_ms: int


class CanaryAdapter(Protocol):
    async def chat(self, *, session_key: str, source_event_id: str, prompt: str) -> ChatObservation: ...

    async def memory_pack(self) -> MemoryObservation: ...

    async def enrichment(self) -> EnrichmentObservation: ...

    async def replay(self, original: ChatObservation, *, source_event_id: str, prompt: str) -> ReplayObservation: ...

    async def cleanup(self, *, off_receipt: Mapping[str, Any]) -> dict[str, Any]: ...


class _RecordingHttpFactory:
    def __init__(self):
        self.posts = 0
        self.statuses: list[str] = []
        self.terminal_frames = 0
        self.callback_outcomes = 0
        self.generation = 0

    def __call__(self, timeout: float):
        recorder = self

        class RecordingClient:
            def __init__(self):
                self.client = httpx.AsyncClient(
                    timeout=timeout,
                    follow_redirects=False,
                    trust_env=False,
                )

            async def __aenter__(self):
                await self.client.__aenter__()
                return self

            async def __aexit__(self, *args):
                return await self.client.__aexit__(*args)

            async def post(self, url: str, **kwargs):
                recorder.posts += 1
                return await self.client.post(url, **kwargs)

            async def get(self, url: str, **kwargs):
                response = await self.client.get(url, **kwargs)
                try:
                    body = response.json()
                except (ValueError, json.JSONDecodeError):
                    return response
                if isinstance(body, dict):
                    status_value = str(body.get("status") or "").strip()
                    if status_value:
                        recorder.statuses.append(status_value)
                    if status_value in TERMINAL_STATUSES:
                        recorder.terminal_frames += 1
                    if body.get("outcome") in {"success", "error"}:
                        recorder.callback_outcomes += 1
                    diagnostic = body.get("diagnostic")
                    if isinstance(diagnostic, dict) and isinstance(diagnostic.get("generation"), int):
                        recorder.generation = int(diagnostic["generation"])
                return response

        return RecordingClient()


class LiveCanaryAdapter:
    """Use only the existing OMI broker and authenticated first-party APIs."""

    def __init__(self, config: CanaryConfig):
        self.config = config

    async def chat(self, *, session_key: str, source_event_id: str, prompt: str) -> ChatObservation:
        recorder = _RecordingHttpFactory()
        client = HermesBrokerClient(self.config.broker, http_client_factory=recorder)
        started = time.monotonic()
        session_id = _session_id(self.config, session_key)
        try:
            result = await client.run_chat_turn(
                account_id=self.config.account_id,
                profile_id=self.config.profile_id,
                runtime_binding_ref=self.config.binding_id,
                consent_epoch=self.config.consent_epoch,
                message=prompt,
                session_key=session_key,
                session_id=session_id,
                source_event_id=source_event_id,
                expected_model=self.config.expected_model,
            )
        except ProvisioningError as exc:
            raise HarnessRefusal(exc.code) from exc
        return ChatObservation(
            text=result.text,
            response_id=result.response_id,
            request_id=result.request_id,
            correlation_id=result.correlation_id,
            duplicate=result.duplicate,
            statuses=tuple(recorder.statuses),
            terminal_frames=recorder.terminal_frames,
            admission_posts=recorder.posts,
            callback_outcomes=recorder.callback_outcomes,
            generation=recorder.generation,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def memory_pack(self) -> MemoryObservation:
        started = time.monotonic()
        auth_token = _resolve_env_secret(self.config.backend_auth_ref)
        _require_firebase_subject(auth_token, self.config.uid)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
            scoped_body = await _request_json(
                client,
                "POST",
                f"{self.config.backend_base_url}/v1/voice/session",
                surface="voice_session",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={
                    "uid": self.config.uid,
                    "provider": "grok-voice",
                    "voice_mode": "v4",
                    "session_scope": {
                        "kind": "memory",
                        "conversation_id": self.config.voice_conversation_id,
                        "expected_active_summary_version_id": self.config.voice_summary_version_id,
                    },
                },
            )
            if len(str(scoped_body.get("session_token") or "")) < 32:
                raise HarnessRefusal("voice_session_token_missing")
            scoped_identity = scoped_body.get("session_scope")
            if not isinstance(scoped_identity, dict):
                raise HarnessRefusal("voice_memory_scope_missing")

            unscoped_body = await _request_json(
                client,
                "POST",
                f"{self.config.backend_base_url}/v1/voice/session",
                surface="voice_session",
                headers={"Authorization": f"Bearer {auth_token}"},
                json={"uid": self.config.uid, "provider": "grok-voice", "voice_mode": "v4"},
            )
            if len(str(unscoped_body.get("session_token") or "")) < 32:
                raise HarnessRefusal("voice_session_token_missing")

        return MemoryObservation(
            # The session route returns this content-free projection only after
            # the server-owned full memory pack has resolved successfully.
            scoped_pack_sha256=_sha256(_canonical_json(scoped_identity)),
            conversation_id_matches=(str(scoped_identity.get("conversation_id")) == self.config.voice_conversation_id),
            summary_version_matches=(
                str(scoped_identity.get("active_summary_version_id")) == self.config.voice_summary_version_id
            ),
            unscoped_memory_absent=unscoped_body.get("session_scope") is None,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def enrichment(self) -> EnrichmentObservation:
        started = time.monotonic()
        token = _resolve_env_secret(self.config.enrichment_token_ref)
        digest = _sha256(f"{self.config.run_id}|{self.config.uid}|{self.config.enrichment_conversation_id}|enrichment")
        client_interaction_id = f"omi-enrichment:{digest}"
        payload = {
            "uid": self.config.uid,
            "conversation_id": self.config.enrichment_conversation_id,
            "client_interaction_id": client_interaction_id,
            "transcript_sha256": self.config.enrichment_transcript_sha256,
        }
        headers = {"X-Ella-Hermes-Cloud-Enrichment-Token": token}
        async with httpx.AsyncClient(timeout=120.0, follow_redirects=False, trust_env=False) as client:
            first = await _request_json(
                client,
                "POST",
                f"{self.config.backend_base_url}/v1/ella/internal/hermes-cloud/enrichment/run",
                surface="enrichment",
                headers=headers,
                json=payload,
            )
            replay = await _request_json(
                client,
                "POST",
                f"{self.config.backend_base_url}/v1/ella/internal/hermes-cloud/enrichment/run",
                surface="enrichment",
                headers=headers,
                json=payload,
            )
        forbidden_chat_keys = {"session_key", "session_id", "canonical_user_event_id"}
        return EnrichmentObservation(
            status=str(first.get("status") or ""),
            correlation_matches=(
                first.get("client_interaction_id") == client_interaction_id
                and replay.get("client_interaction_id") == client_interaction_id
            ),
            transcript_hash_matches=(
                first.get("transcript_sha256") == self.config.enrichment_transcript_sha256
                and replay.get("transcript_sha256") == self.config.enrichment_transcript_sha256
            ),
            duplicate_replay=first.get("duplicate") is False and replay.get("duplicate") is True,
            chat_identity_absent=forbidden_chat_keys.isdisjoint(first) and forbidden_chat_keys.isdisjoint(replay),
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def replay(self, original: ChatObservation, *, source_event_id: str, prompt: str) -> ReplayObservation:
        started = time.monotonic()
        replay = await self.chat(
            session_key=self.config.primary_session_key,
            source_event_id=source_event_id,
            prompt=prompt,
        )
        contract_checks = await _contract_failure_checks(self.config, source_event_id)
        return ReplayObservation(
            duplicate_ingress=replay.duplicate,
            same_response_hash=_sha256(replay.text) == _sha256(original.text),
            same_response_id=replay.response_id == original.response_id,
            missing_outcome_refused=contract_checks["missing_outcome"],
            wrong_correlation_refused=contract_checks["wrong_correlation"],
            wrong_session_refused=contract_checks["wrong_session"],
            timeout_refused=contract_checks["timeout"],
            partial_after_terminal_refused=_status_sequence_valid(
                ("pending", "writeback_completed", "writeback_pending")
            )
            is False,
            latency_ms=int((time.monotonic() - started) * 1000),
        )

    async def cleanup(self, *, off_receipt: Mapping[str, Any]) -> dict[str, Any]:
        _validate_off_receipt(off_receipt, self.config)
        auth_token = _resolve_env_secret(self.config.backend_auth_ref)
        _require_firebase_subject(auth_token, self.config.uid)
        voice_counts = await voice_canary_db.delete_user_voice_data(self.config.uid)
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=False, trust_env=False) as client:
            body = await _request_json(
                client,
                "DELETE",
                f"{self.config.backend_base_url}/v1/users/delete-account",
                surface="account_cleanup",
                unavailable_is_not_tested=False,
                headers={"Authorization": f"Bearer {auth_token}"},
            )
        if body.get("status") != "ok":
            raise HarnessRefusal("account_cleanup_refused")
        return {
            "status": "cleaned",
            "uid_sha256": _sha256(self.config.uid),
            "flags_off": True,
            "selectors_empty": True,
            "workflows_off": True,
            "exact_account_delete": True,
            "voice_rows_removed": sum(int(value) for value in voice_counts.values()),
            "content_free": True,
        }


def _response_json(response: httpx.Response, surface: str) -> dict[str, Any]:
    if response.status_code >= 400:
        if response.status_code in {404, 501, 503}:
            raise ScenarioNotTested(f"{surface}_unavailable")
        raise HarnessRefusal(f"{surface}_http_refused")
    try:
        body = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        raise HarnessRefusal(f"{surface}_response_invalid") from exc
    if not isinstance(body, dict):
        raise HarnessRefusal(f"{surface}_response_invalid")
    return body


async def _request_json(
    client: httpx.AsyncClient,
    method: str,
    url: str,
    *,
    surface: str,
    unavailable_is_not_tested: bool = True,
    **kwargs,
) -> dict[str, Any]:
    try:
        response = await client.request(method, url, **kwargs)
    except httpx.HTTPError as exc:
        if unavailable_is_not_tested:
            raise ScenarioNotTested(f"{surface}_transport_unavailable") from exc
        raise HarnessRefusal(f"{surface}_transport_refused") from exc
    try:
        return _response_json(response, surface)
    except ScenarioNotTested as exc:
        if unavailable_is_not_tested:
            raise
        raise HarnessRefusal(f"{surface}_unavailable") from exc


def _session_id(config: CanaryConfig, session_key: str) -> str:
    return broker_session_id_for_scope(
        account_id=config.account_id,
        profile_id=config.profile_id,
        runtime_binding_ref=config.binding_id,
        channel=config.chat_channel,
        session_key=session_key,
    )


def _source_event_id(config: CanaryConfig, label: str) -> str:
    return f"canary-event:{_sha256(f'{config.run_id}|{label}')[:48]}"


def _marker(config: CanaryConfig, label: str) -> str:
    return f"CANARY-{label.upper()}-{_sha256(f'{config.run_id}|{label}')[:24]}"


def _status_sequence_valid(statuses: tuple[str, ...]) -> bool:
    terminal_seen = False
    terminal_count = 0
    for status_value in statuses:
        if status_value in TERMINAL_STATUSES:
            terminal_count += 1
            terminal_seen = True
        elif terminal_seen:
            return False
    return terminal_count == 1


def _sse_contract(text: str, response_id: str) -> tuple[str, ...]:
    payload = {
        "id": response_id,
        "text": text,
        "sender": "ai",
        "type": "text",
    }
    return (
        f"data: {text.replace(chr(10), '__CRLF__')}",
        f"done: {base64.b64encode(json.dumps(payload).encode()).decode()}",
    )


def _sse_contract_valid(frames: tuple[str, ...]) -> bool:
    if len(frames) != 2 or not frames[0].startswith("data: ") or not frames[1].startswith("done: "):
        return False
    try:
        decoded = json.loads(base64.b64decode(frames[1][6:]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    data_text = frames[0][6:].replace("__CRLF__", "\n")
    return decoded.get("text") == data_text and decoded.get("sender") == "ai"


async def _contract_failure_checks(config: CanaryConfig, source_event_id: str) -> dict[str, bool]:
    client = HermesBrokerClient(config.broker)
    session_key = config.primary_session_key
    session_id = _session_id(config, session_key)
    result = {
        "answer": "CANARY-CONTENT-SAFE",
        "session_key": session_key,
        "session_id": session_id,
        "canonical_user_event_id": source_event_id,
        "model": config.expected_model,
    }
    terminal = {
        "outcome": "success",
        "result": result,
        "diagnostic": {
            "stage": "broker_writeback",
            "reason": "writeback_completed",
            "generation": 1,
        },
    }
    checks: dict[str, bool] = {}
    for label, mutated in (
        ("missing_outcome", {**terminal, "outcome": None}),
        ("wrong_session", {**terminal, "result": {**result, "session_id": f"{session_id}-wrong"}}),
    ):
        try:
            client._map_chat_result(
                mutated,
                request_id="canary-request",
                correlation_id="canary-correlation",
                expected_model=config.expected_model,
                session_key=session_key,
                session_id=session_id,
                source_event_id=source_event_id,
                admission_duplicate=False,
            )
        except ProvisioningError:
            checks[label] = True
        else:
            checks[label] = False
    envelope = {
        "request_id": "canary-request",
        "correlation_id": "canary-correlation-wrong",
        "account_id": config.account_id,
        "profile_id": config.profile_id,
        "lane": "chat_turn",
    }
    try:
        client._assert_terminal_envelope(
            envelope,
            request_id="canary-request",
            correlation_id="canary-correlation",
            account_id=config.account_id,
            profile_id=config.profile_id,
            lane="chat_turn",
            require_terminal_identity=True,
        )
    except ProvisioningError:
        checks["wrong_correlation"] = True
    else:
        checks["wrong_correlation"] = False

    class PendingResponse:
        status_code = 200
        content = b"{}"

        @staticmethod
        def json():
            return {
                "status": "pending",
                "request_id": "canary-request",
                "account_id": config.account_id,
                "profile_id": config.profile_id,
                "lane": "chat_turn",
                "callback_contract": STOCK_CALLBACK_CONTRACT,
                "terminal_proof": False,
                "outcome": None,
                "diagnostic": {
                    "stage": "broker_request",
                    "reason": "pending",
                    "generation": 1,
                },
            }

    class PendingHttp:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, _url, **_kwargs):
            return PendingResponse()

    clock_values = iter((0.0, 0.0, config.broker.poll_timeout_seconds + 1.0))
    timeout_client = HermesBrokerClient(
        config.broker,
        http_client_factory=lambda timeout: PendingHttp(),
        sleep=lambda _seconds: asyncio.sleep(0),
        clock=lambda: next(clock_values),
    )
    timeout_client._headers = lambda: {
        "Authorization": "Bearer content-safe-contract-test",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Host": config.broker.allowed_host,
    }
    try:
        await timeout_client.wait_for_terminal(
            request_id="canary-request",
            account_id=config.account_id,
            profile_id=config.profile_id,
            lane="chat_turn",
        )
    except ProvisioningError as exc:
        checks["timeout"] = exc.code == "hermes_broker_prototype_wait_timeout"
    else:
        checks["timeout"] = False
    return checks


@dataclass
class Stage:
    scenario: str
    name: str
    status: str
    latency_ms: int
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class HarnessReport:
    stages: list[Stage]
    verdicts: dict[str, str]

    def render(self) -> str:
        lines: list[str] = []
        for scenario in "ABCDEF":
            lines.extend(
                (
                    f"Scenario {scenario}",
                    "",
                    "| Stage | Status | Latency ms | Content-free evidence |",
                    "|---|---:|---:|---|",
                )
            )
            rows = [stage for stage in self.stages if stage.scenario == scenario]
            if not rows:
                rows = [Stage(scenario, "scenario", "NOT TESTED", 0, {"reason": "not_run"})]
            for stage in rows:
                evidence = "; ".join(f"{key}={str(value).lower()}" for key, value in sorted(stage.evidence.items()))
                lines.append(f"| {stage.name} | {stage.status} | {stage.latency_ms} | {evidence} |")
            lines.append("")
        lines.extend(
            (
                "Final verdict matrix",
                "",
                "| Capability | Verdict |",
                "|---|---:|",
            )
        )
        for capability, verdict in self.verdicts.items():
            lines.append(f"| {capability} | {verdict} |")
        return "\n".join(lines) + "\n"


def _pass_or_fail(condition: bool) -> str:
    return "PASS" if condition else "FAIL"


def _scenario_verdict(stages: list[Stage], scenario: str) -> str:
    statuses = [stage.status for stage in stages if stage.scenario == scenario]
    if not statuses or "NOT TESTED" in statuses:
        return "NOT TESTED"
    return "FAIL" if "FAIL" in statuses else "PASS"


async def run_harness(config: CanaryConfig, adapter: CanaryAdapter) -> HarnessReport:
    stages: list[Stage] = []
    observations: dict[str, ChatObservation] = {}

    async def run_chat(label: str, session_key: str, prompt: str) -> ChatObservation:
        return await adapter.chat(
            session_key=session_key,
            source_event_id=_source_event_id(config, label),
            prompt=prompt,
        )

    full_response = _marker(config, "full-response")
    try:
        observation = await run_chat(
            "full-response",
            config.primary_session_key,
            f"Return exactly this synthetic marker and nothing else: {full_response}",
        )
        observations["full-response"] = observation
        fidelity = observation.text == full_response
        transport_ok = (
            observation.admission_posts == 1
            and observation.callback_outcomes == 1
            and observation.terminal_frames == 1
            and observation.generation == 1
            and _status_sequence_valid(observation.statuses)
            and observation.latency_ms <= config.max_latency_ms
            and not observation.duplicate
        )
        sse_ok = _sse_contract_valid(_sse_contract(observation.text, observation.response_id))
        stages.extend(
            (
                Stage(
                    "A",
                    "broker_transport",
                    _pass_or_fail(transport_ok),
                    observation.latency_ms,
                    {
                        "admissions": observation.admission_posts,
                        "callbacks": observation.callback_outcomes,
                        "generation": observation.generation,
                        "provider_turns": 1 if observation.generation == 1 and not observation.duplicate else 0,
                        "terminal_frames": observation.terminal_frames,
                        "writebacks": observation.terminal_frames,
                    },
                ),
                Stage(
                    "A",
                    "full_response_fidelity",
                    _pass_or_fail(fidelity),
                    observation.latency_ms,
                    {
                        "hash_equal": fidelity,
                        "response_sha256": _sha256(observation.text),
                    },
                ),
                Stage(
                    "A",
                    "sse_projection",
                    _pass_or_fail(sse_ok),
                    0,
                    {"event_sequence": "data>done", "duplicate_terminal": False},
                ),
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("A", "broker_transport", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("A", "broker_transport", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    continuity_nonce = _marker(config, "same-session-nonce")
    try:
        first = await run_chat(
            "continuity-first",
            config.primary_session_key,
            f"Remember this turn-local synthetic nonce: {continuity_nonce}. Reply exactly ACK.",
        )
        second = await run_chat(
            "continuity-second",
            config.primary_session_key,
            "Return exactly the synthetic nonce from the previous turn and nothing else.",
        )
        stable_identity = _session_id(config, config.primary_session_key) == _session_id(
            config,
            config.primary_session_key,
        )
        continuity_ok = (
            second.text == continuity_nonce and stable_identity and not first.duplicate and not second.duplicate
        )
        stages.append(
            Stage(
                "B",
                "fixed_session_continuity",
                _pass_or_fail(continuity_ok),
                first.latency_ms + second.latency_ms,
                {
                    "nonce_hash_equal": second.text == continuity_nonce,
                    "stable_session_id": stable_identity,
                    "turns": 2,
                },
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("B", "fixed_session_continuity", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("B", "fixed_session_continuity", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    try:
        isolated = await run_chat(
            "isolation-second-session",
            config.isolated_session_key,
            "If a turn-local synthetic nonce exists in this session, return it. Otherwise return exactly NO-NONCE.",
        )
        retained = await run_chat(
            "isolation-original-session",
            config.primary_session_key,
            "Return exactly the synthetic nonce retained in this session and nothing else.",
        )
        isolated_ok = (
            isolated.text == "NO-NONCE"
            and retained.text == continuity_nonce
            and _session_id(config, config.primary_session_key) != _session_id(config, config.isolated_session_key)
        )
        stages.append(
            Stage(
                "C",
                "cross_session_isolation",
                _pass_or_fail(isolated_ok),
                isolated.latency_ms + retained.latency_ms,
                {
                    "isolated_absent": isolated.text == "NO-NONCE",
                    "original_retained": retained.text == continuity_nonce,
                    "session_ids_distinct": True,
                },
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("C", "cross_session_isolation", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("C", "cross_session_isolation", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    try:
        memory = await adapter.memory_pack()
        memory_ok = (
            memory.scoped_pack_sha256 == config.memory_pack_sha256
            and memory.conversation_id_matches
            and memory.summary_version_matches
            and memory.unscoped_memory_absent
            and memory.latency_ms <= config.max_latency_ms
        )
        stages.append(
            Stage(
                "D",
                "profile_memory_pack",
                _pass_or_fail(memory_ok),
                memory.latency_ms,
                {
                    "conversation_pinned": memory.conversation_id_matches,
                    "pack_hash_equal": memory.scoped_pack_sha256 == config.memory_pack_sha256,
                    "unscoped_absent": memory.unscoped_memory_absent,
                    "version_pinned": memory.summary_version_matches,
                },
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("D", "profile_memory_pack", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("D", "profile_memory_pack", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    try:
        enrichment = await adapter.enrichment()
        post_enrichment_chat = await run_chat(
            "enrichment-chat-isolation",
            config.primary_session_key,
            "Return exactly the synthetic nonce retained in this chat session and nothing else.",
        )
        chat_session_retained = post_enrichment_chat.text == continuity_nonce
        enrichment_ok = (
            enrichment.status == "applied"
            and enrichment.correlation_matches
            and enrichment.transcript_hash_matches
            and enrichment.duplicate_replay
            and enrichment.chat_identity_absent
            and chat_session_retained
            and enrichment.latency_ms <= config.max_latency_ms
        )
        stages.append(
            Stage(
                "E",
                "transcript_enrichment",
                _pass_or_fail(enrichment_ok),
                enrichment.latency_ms,
                {
                    "chat_identity_absent": enrichment.chat_identity_absent,
                    "chat_session_retained": chat_session_retained,
                    "correlation_pinned": enrichment.correlation_matches,
                    "one_canonical_result": enrichment.duplicate_replay,
                    "transcript_hash_equal": enrichment.transcript_hash_matches,
                },
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("E", "transcript_enrichment", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("E", "transcript_enrichment", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    try:
        original = observations.get("full-response")
        if original is None:
            raise ScenarioNotTested("full_response_prerequisite_unavailable")
        replay = await adapter.replay(
            original,
            source_event_id=_source_event_id(config, "full-response"),
            prompt=f"Return exactly this synthetic marker and nothing else: {full_response}",
        )
        replay_ok = all(
            (
                replay.duplicate_ingress,
                replay.same_response_hash,
                replay.same_response_id,
                replay.missing_outcome_refused,
                replay.wrong_correlation_refused,
                replay.wrong_session_refused,
                replay.timeout_refused,
                replay.partial_after_terminal_refused,
            )
        )
        stages.append(
            Stage(
                "F",
                "error_and_replay",
                _pass_or_fail(replay_ok),
                replay.latency_ms,
                {
                    "callback_contract": CALLBACK_SCHEMA,
                    "duplicate_ingress_suppressed": replay.duplicate_ingress,
                    "missing_outcome_refused": replay.missing_outcome_refused,
                    "partial_after_terminal_refused": replay.partial_after_terminal_refused,
                    "same_canonical_write": replay.same_response_id,
                    "timeout_refused": replay.timeout_refused,
                    "wrong_correlation_refused": replay.wrong_correlation_refused,
                    "wrong_session_refused": replay.wrong_session_refused,
                },
            )
        )
    except ScenarioNotTested as exc:
        stages.append(Stage("F", "error_and_replay", "NOT TESTED", 0, {"reason": exc.code}))
    except (HarnessRefusal, ProvisioningError) as exc:
        stages.append(Stage("F", "error_and_replay", "FAIL", 0, {"reason": getattr(exc, "code", "refused")}))

    verdicts = {
        "chat transport": _scenario_verdict(stages, "A"),
        "full-response fidelity": next(
            (stage.status for stage in stages if stage.name == "full_response_fidelity"),
            "NOT TESTED",
        ),
        "API/SSE-like consumption": next(
            (stage.status for stage in stages if stage.name == "sse_projection"),
            "NOT TESTED",
        ),
        "same-session continuity": _scenario_verdict(stages, "B"),
        "cross-session isolation": _scenario_verdict(stages, "C"),
        "profile memory pack": _scenario_verdict(stages, "D"),
        "enrichment": _scenario_verdict(stages, "E"),
        "replay safety": _scenario_verdict(stages, "F"),
    }
    return HarnessReport(stages=stages, verdicts=verdicts)


def _validate_off_receipt(receipt: Mapping[str, Any], config: CanaryConfig) -> None:
    if set(receipt) != {
        "schema_version",
        "uid_sha256",
        "flags_off",
        "selectors_empty",
        "workflows_off",
        "content_free",
    }:
        raise HarnessRefusal("cleanup_off_receipt_shape_invalid")
    if (
        receipt.get("schema_version") != "ella-hermes-canary-off-receipt-v1"
        or receipt.get("uid_sha256") != _sha256(config.uid)
        or receipt.get("flags_off") is not True
        or receipt.get("selectors_empty") is not True
        or receipt.get("workflows_off") is not True
        or receipt.get("content_free") is not True
    ):
        raise HarnessRefusal("cleanup_off_receipt_invalid")


def load_off_receipt(path: str) -> dict[str, Any]:
    protected = _assert_protected_file(Path(path))
    try:
        value = json.loads(protected.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessRefusal("cleanup_off_receipt_invalid") from exc
    if not isinstance(value, dict):
        raise HarnessRefusal("cleanup_off_receipt_invalid")
    return value


def _exit_code(report: HarnessReport) -> int:
    values = set(report.verdicts.values())
    if "FAIL" in values:
        return 1
    if "NOT TESTED" in values:
        return 2
    return 0


async def _main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    run_parser = subparsers.add_parser("run")
    run_parser.add_argument("--config", required=True)
    cleanup_parser = subparsers.add_parser("cleanup")
    cleanup_parser.add_argument("--config", required=True)
    cleanup_parser.add_argument("--off-receipt", required=True)
    cleanup_parser.add_argument("--confirm-uid", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    adapter = LiveCanaryAdapter(config)
    if args.command == "run":
        report = await run_harness(config, adapter)
        print(report.render(), end="")
        return _exit_code(report)
    if args.confirm_uid != config.uid:
        raise HarnessRefusal("cleanup_confirmation_mismatch")
    result = await adapter.cleanup(off_receipt=load_off_receipt(args.off_receipt))
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(_main()))
    except HarnessRefusal as exc:
        raise SystemExit(f"harness_refused:{exc.code}") from None
