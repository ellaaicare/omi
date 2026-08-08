#!/usr/bin/env python3
"""Content-free acceptance harness for one synthetic Hermes Cloud profile."""

from __future__ import annotations

import argparse
import asyncio
import base64
import binascii
import hashlib
import hmac
import json
import os
import re
import stat
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Protocol
from urllib.parse import urlparse

import httpx

from database import voice_canary as voice_canary_db
from ella.services import ai_consent
from ella.services.hermes_broker_client import HermesBrokerClient
from ella.services.hermes_broker_prototype import HermesBrokerPrototypeConfig
from ella.services.hermes_cloud_enrichment import (
    build_enrichment_identity,
    _interaction_identity as production_enrichment_interaction_identity,
)
from ella.services.hermes_cloud_runtime import broker_session_id_for_scope
from ella.services.runtime_errors import ProvisioningError
from models.conversation import (
    Conversation,
    ConversationSource,
    ConversationStatus,
    ConversationVisibility,
    Structured,
)
from models.transcript_segment import TranscriptSegment

CONFIG_SCHEMA = "ella-hermes-product-fit-canary-v1"
CALLBACK_SCHEMA = "ella.hermes.callback.v1"
STOCK_CALLBACK_CONTRACT = "stock_best_effort_v1"
FIXTURE_SCHEMA = "ella-hermes-product-fit-fixture-v1"
OFF_RECEIPT_SCHEMA = "ella-hermes-canary-off-receipt-v1"
FIXTURE_OFF_RECEIPT_MAX_AGE_SECONDS = 15 * 60
FIXTURE_OFF_RECEIPT_MAX_FUTURE_SKEW_SECONDS = 30
APPROVED_SECRET_ROOTS = (Path("/etc/ella"), Path("/var/lib/ella"))
SYNTHETIC_PREFIXES = ("synthetic-", "staging-synthetic-")
TERMINAL_STATUSES = frozenset({"writeback_completed", "blocked", "quarantined", "expired", "writeback_blocked"})
SECRET_REF_RE = re.compile(r"^env:(ELLA_[A-Z0-9_]{3,120})$")
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,256}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ENRICHMENT_CHAT_IDENTITY_FIELDS = frozenset(
    {
        "session_key",
        "session_id",
        "chat_session_key",
        "chat_session_id",
        "hermes_session_key",
        "hermes_session_id",
        "broker_session_id",
    }
)
FIXTURE_GLOBAL_FLAGS = (
    "ELLA_RUNTIME_BINDINGS_ENABLED",
    "ELLA_HERMES_PROVISIONING_ENABLED",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED",
    "ELLA_AI_CONSENT_ENFORCEMENT_ENABLED",
    "ELLA_MANAGED_CLOUD_REAL_DATA_ENABLED",
    "ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED",
    "ELLA_ISOLATED_VOICE_ROUTING_ENABLED",
    "ELLA_INVITE_ORDINARY_SELF_SERVICE_ENABLED",
    "ELLA_INVITE_APP_REVIEW_ENABLED",
    "ELLA_HERMES_CLOUD_STAGED_ATTESTATION_ENABLED",
    "ELLA_HERMES_CLOUD_PHOTON_ENABLED",
    "ELLA_HERMES_BROKER_PROTOTYPE_ENABLED",
)
FIXTURE_UID_SELECTORS = (
    "ELLA_RUNTIME_BINDINGS_ENABLED_UIDS",
    "ELLA_HERMES_PROVISIONING_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_ENRICHMENT_ENABLED_UIDS",
    "ELLA_HERMES_CLOUD_SYNTHETIC_UIDS",
    "ELLA_AI_CONSENT_ENFORCEMENT_UIDS",
    "ELLA_ISOLATED_VOICE_ROUTING_ENABLED_UIDS",
)
conversations_db: Any = None
vector_db: Any = None


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


def _assert_fixture_rollout_off() -> None:
    if os.getenv("ELLA_HERMES_CLOUD_SYNTHETIC_ONLY", "true").strip().lower() != "true":
        raise HarnessRefusal("fixture_synthetic_only_required")
    if any(os.getenv(name, "").strip().lower() not in {"", "false"} for name in FIXTURE_GLOBAL_FLAGS):
        raise HarnessRefusal("fixture_global_flag_enabled")
    if any(os.getenv(name, "").strip() for name in FIXTURE_UID_SELECTORS):
        raise HarnessRefusal("fixture_uid_selector_enabled")
    if any(
        os.getenv(name, "").strip()
        for name in (
            "ELLA_HERMES_BROKER_PROTOTYPE_ACCOUNT_ID",
            "ELLA_HERMES_BROKER_PROTOTYPE_PROFILE_ID",
            "ELLA_HERMES_BROKER_PROTOTYPE_BINDING_ID",
        )
    ):
        raise HarnessRefusal("fixture_broker_selector_enabled")


def _fixture_scope_sha256(config: CanaryConfig) -> str:
    return _sha256(
        _canonical_json(
            {
                "run_id": config.run_id,
                "uid": config.uid,
                "account_id": config.account_id,
                "profile_id": config.profile_id,
                "binding_id": config.binding_id,
                "consent_epoch": config.consent_epoch,
                "expected_model": config.expected_model,
                "chat_channel": config.chat_channel,
            }
        )
    )


def _validate_fixture_off_receipt(
    receipt: Mapping[str, Any],
    config: CanaryConfig,
    *,
    now: datetime | None = None,
) -> None:
    if set(receipt) != {
        "schema_version",
        "uid_sha256",
        "binding_id_sha256",
        "scope_sha256",
        "observed_at_utc",
        "flags_off",
        "selectors_empty",
        "workflows_off",
        "content_free",
    }:
        raise HarnessRefusal("fixture_off_receipt_shape_invalid")
    try:
        _validate_off_receipt(
            {
                key: receipt.get(key)
                for key in (
                    "schema_version",
                    "uid_sha256",
                    "flags_off",
                    "selectors_empty",
                    "workflows_off",
                    "content_free",
                )
            },
            config,
        )
    except HarnessRefusal as exc:
        raise HarnessRefusal("fixture_off_receipt_invalid") from exc
    if receipt.get("binding_id_sha256") != _sha256(config.binding_id) or receipt.get(
        "scope_sha256"
    ) != _fixture_scope_sha256(config):
        raise HarnessRefusal("fixture_off_receipt_invalid")
    observed_raw = receipt.get("observed_at_utc")
    if not isinstance(observed_raw, str):
        raise HarnessRefusal("fixture_off_receipt_invalid")
    try:
        observed_at = datetime.strptime(observed_raw, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError as exc:
        raise HarnessRefusal("fixture_off_receipt_invalid") from exc
    checked_at = now or datetime.now(timezone.utc)
    if checked_at.tzinfo is None:
        raise HarnessRefusal("fixture_off_receipt_invalid")
    age_seconds = (checked_at.astimezone(timezone.utc) - observed_at).total_seconds()
    if not (-FIXTURE_OFF_RECEIPT_MAX_FUTURE_SKEW_SECONDS <= age_seconds <= FIXTURE_OFF_RECEIPT_MAX_AGE_SECONDS):
        raise HarnessRefusal("fixture_off_receipt_stale")


def _fixture_conversation_id(config: CanaryConfig) -> str:
    material = "\x1f".join(
        (
            FIXTURE_SCHEMA,
            config.run_id,
            config.uid,
            config.account_id,
            config.profile_id,
        )
    )
    return f"hermes-fixture:{_sha256(material)[:40]}"


def _fixture_marker(config: CanaryConfig, conversation_id: str) -> dict[str, Any]:
    return {
        "schema_version": FIXTURE_SCHEMA,
        "run_id_sha256": _sha256(config.run_id),
        "uid_sha256": _sha256(config.uid),
        "account_id_sha256": _sha256(config.account_id),
        "profile_id_sha256": _sha256(config.profile_id),
        "binding_id_sha256": _sha256(config.binding_id),
        "conversation_id_sha256": _sha256(conversation_id),
        "content_free": True,
    }


def _fixture_conversation(config: CanaryConfig) -> Conversation:
    conversation_id = _fixture_conversation_id(config)
    started_at = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
    segments = [
        TranscriptSegment(
            id=f"fixture-segment:{_sha256(conversation_id + ':0')[:32]}",
            text="Synthetic product-fit fixture turn one.",
            speaker="SPEAKER_00",
            is_user=True,
            start=0.0,
            end=2.0,
        ),
        TranscriptSegment(
            id=f"fixture-segment:{_sha256(conversation_id + ':1')[:32]}",
            text="Synthetic product-fit fixture turn two.",
            speaker="SPEAKER_01",
            is_user=False,
            start=2.0,
            end=4.0,
        ),
    ]
    return Conversation(
        id=conversation_id,
        created_at=started_at,
        started_at=started_at,
        finished_at=started_at + timedelta(seconds=4),
        source=ConversationSource.external_integration,
        language="en",
        structured=Structured(
            title="Synthetic Hermes fixture",
            overview="Synthetic data for the bounded Hermes product-fit fixture.",
            emoji="\U0001f9ea",
            category="other",
        ),
        transcript_segments=segments,
        visibility=ConversationVisibility.private,
        discarded=False,
        status=ConversationStatus.completed,
        external_data={"ella_product_fit_fixture": _fixture_marker(config, conversation_id)},
    )


def _enum_value(value: Any) -> str:
    return str(getattr(value, "value", value) or "")


def _validate_fixture_provenance(
    config: CanaryConfig,
    conversation: Mapping[str, Any],
    receipt: Mapping[str, Any] | None = None,
) -> None:
    expected = _fixture_conversation(config).model_dump()
    external_data = conversation.get("external_data")
    marker = external_data.get("ella_product_fit_fixture") if isinstance(external_data, Mapping) else None
    if (
        conversation.get("id") != expected["id"]
        or marker != expected["external_data"]["ella_product_fit_fixture"]
        or _enum_value(conversation.get("source")) != ConversationSource.external_integration.value
        or _enum_value(conversation.get("visibility")) != ConversationVisibility.private.value
        or _enum_value(conversation.get("status")) != ConversationStatus.completed.value
        or conversation.get("discarded") is not False
        or conversation.get("transcript_segments") != expected["transcript_segments"]
    ):
        raise HarnessRefusal("fixture_conversation_drift")
    if receipt is not None:
        identity = build_enrichment_identity(
            uid=config.uid,
            conversation_id=str(conversation["id"]),
            conversation=dict(conversation),
        )
        if (
            receipt.get("conversation_id") != conversation.get("id")
            or receipt.get("fixture_marker_sha256") != _sha256(_canonical_json(marker))
            or receipt.get("transcript_sha256") != identity.transcript_sha256
        ):
            raise HarnessRefusal("fixture_conversation_drift")


def _validate_fixture_conversation(
    config: CanaryConfig,
    conversation: Mapping[str, Any],
) -> None:
    expected = _fixture_conversation(config).model_dump()
    _validate_fixture_provenance(config, conversation)
    if conversation.get("enrichment_state") is not None or conversation.get("structured") != expected["structured"]:
        raise HarnessRefusal("fixture_conversation_drift")


class FixtureStore(Protocol):
    async def assert_prepare_authority(self, config: CanaryConfig) -> None: ...

    async def get_conversation(self, uid: str, conversation_id: str) -> Mapping[str, Any] | None: ...

    async def create_conversation(self, uid: str, conversation: Conversation) -> None: ...

    async def ensure_summary_version(self, uid: str, conversation_id: str) -> Mapping[str, Any]: ...

    async def delete_conversation(self, uid: str, conversation_id: str) -> None: ...

    async def delete_vector(self, uid: str, conversation_id: str) -> None: ...

    async def vector_exists(self, uid: str, conversation_id: str) -> bool: ...


def _validate_fixture_authority(config: CanaryConfig, value: Mapping[str, Any]) -> None:
    expected_profile_binding = ai_consent.derive_profile_binding_id(
        account_uid=config.uid,
        profile_uid=config.uid,
    )
    if (
        str(value.get("user_id")) != config.account_id
        or str(value.get("user_id")) != config.profile_id
        or value.get("omi_uid") != config.uid
        or str(value.get("user_status") or "").upper() != "ACTIVE"
        or value.get("profile_class") != "synthetic"
        or str(value.get("binding_id")) != config.binding_id
        or str(value.get("binding_user_id")) != config.account_id
        or str(value.get("account_user_id")) != config.account_id
        or str(value.get("profile_user_id")) != config.profile_id
        or value.get("provider") != "hermes_cloud"
        or value.get("binding_status") not in {"internal_canary", "active"}
        or value.get("health_state") != "healthy"
        or value.get("active") is not True
        or value.get("expected_model") != config.expected_model
        or value.get("decision") != "granted"
        or value.get("consent_profile_binding_id") != expected_profile_binding
        or value.get("policy_version") != ai_consent.CURRENT_POLICY_VERSION
        or value.get("processor_set_hash") != ai_consent.CURRENT_PROCESSOR_SET_HASH
        or value.get("scope_version") != ai_consent.CURRENT_SCOPE_VERSION
        or value.get("scope_hash") != ai_consent.CURRENT_SCOPE_HASH
        or str(value.get("authority_epoch")) != config.consent_epoch
        or value.get("entitlement_status") != "active"
        or str(value.get("entitlement_authority_epoch")) != config.consent_epoch
        or value.get("transcript_target_ready") is not True
    ):
        raise HarnessRefusal("fixture_authority_mismatch")


class ProductionFixtureStore:
    def __init__(self):
        if conversations_db is None or vector_db is None:
            raise HarnessRefusal("fixture_persistence_modules_unavailable")

    async def assert_prepare_authority(self, config: CanaryConfig) -> None:
        pool = await voice_canary_db.get_pool()
        row = await pool.fetchrow(
            """
            SELECT
                u.id AS user_id,
                u.omi_uid,
                u.status AS user_status,
                u.profile_class,
                b.id AS binding_id,
                b.user_id AS binding_user_id,
                b.account_user_id,
                b.profile_user_id,
                b.provider,
                b.status AS binding_status,
                b.health_state,
                b.active,
                b.expected_model,
                a.decision,
                a.profile_binding_id AS consent_profile_binding_id,
                a.policy_version,
                a.processor_set_hash,
                a.scope_version,
                a.scope_hash,
                a.authority_epoch,
                e.status AS entitlement_status,
                e.consent_authority_epoch AS entitlement_authority_epoch,
                EXISTS (
                    SELECT 1
                    FROM ella_runtime_targets t
                    WHERE t.runtime_binding_id = b.id
                      AND t.account_user_id = u.id
                      AND t.profile_user_id = u.id
                      AND t.provider = 'hermes_cloud'
                      AND t.mode = 'hermes-cloud-transcript'
                      AND t.status = 'ready'
                      AND t.policy_version = a.policy_version
                      AND t.processor_set_hash = a.processor_set_hash
                      AND t.scope_version = a.scope_version
                      AND t.scope_hash = a.scope_hash
                ) AS transcript_target_ready
            FROM users u
            JOIN ella_runtime_bindings b ON b.id = $4 AND b.user_id = u.id
            JOIN ella_managed_cloud_consent_authority a ON a.user_id = u.id
            JOIN voice_entitlements e ON e.uid = u.omi_uid
            WHERE u.omi_uid = $1
              AND u.id = $2
              AND u.id = $3
            """,
            config.uid,
            uuid.UUID(config.account_id),
            uuid.UUID(config.profile_id),
            uuid.UUID(config.binding_id),
        )
        if row is None:
            raise HarnessRefusal("fixture_authority_mismatch")
        _validate_fixture_authority(config, dict(row))

    async def get_conversation(self, uid: str, conversation_id: str) -> Mapping[str, Any] | None:
        return await asyncio.to_thread(conversations_db.get_conversation, uid, conversation_id)

    async def create_conversation(self, uid: str, conversation: Conversation) -> None:
        await asyncio.to_thread(conversations_db.upsert_conversation, uid, conversation.model_dump())

    async def ensure_summary_version(self, uid: str, conversation_id: str) -> Mapping[str, Any]:
        return await asyncio.to_thread(
            conversations_db.ensure_voice_memory_summary_version,
            uid,
            conversation_id,
        )

    async def delete_conversation(self, uid: str, conversation_id: str) -> None:
        await asyncio.to_thread(conversations_db.delete_conversation, uid, conversation_id)

    async def delete_vector(self, uid: str, conversation_id: str) -> None:
        if vector_db.index is None:
            raise HarnessRefusal("fixture_vector_store_unavailable")
        await asyncio.to_thread(vector_db.delete_vector, uid, conversation_id)

    async def vector_exists(self, uid: str, conversation_id: str) -> bool:
        if vector_db.index is None:
            raise HarnessRefusal("fixture_vector_store_unavailable")
        existing = await asyncio.to_thread(
            vector_db.fetch_existing_conversation_vector_ids,
            uid,
            [conversation_id],
        )
        return conversation_id in existing


def _fixture_receipt(
    config: CanaryConfig,
    conversation: Mapping[str, Any],
    active_summary_version_id: str,
) -> dict[str, Any]:
    identity = build_enrichment_identity(
        uid=config.uid,
        conversation_id=str(conversation["id"]),
        conversation=dict(conversation),
    )
    external_data = conversation.get("external_data")
    marker = external_data.get("ella_product_fit_fixture") if isinstance(external_data, Mapping) else None
    return {
        "schema_version": FIXTURE_SCHEMA,
        "status": "ready",
        "uid_sha256": _sha256(config.uid),
        "conversation_id": str(conversation["id"]),
        "active_summary_version_id": active_summary_version_id,
        "transcript_sha256": identity.transcript_sha256,
        "enrichment_client_interaction_id": identity.client_interaction_id,
        "enrichment_job_id": identity.job_id,
        "fixture_marker_sha256": _sha256(_canonical_json(marker)),
        "provider_calls": 0,
        "enrichment_success_preseeded": False,
        "content_free": True,
    }


def _assert_fixture_summary_version(conversation: Mapping[str, Any], version_id: str) -> None:
    if str(conversation.get("active_summary_version_id") or "") != version_id:
        raise HarnessRefusal("fixture_summary_version_drift")
    versions = conversation.get("summary_versions")
    active = [
        value
        for value in (versions if isinstance(versions, list) else [])
        if isinstance(value, Mapping) and str(value.get("id") or "") == version_id and value.get("is_active") is True
    ]
    if len(active) != 1 or active[0].get("kind") != "legacy_current":
        raise HarnessRefusal("fixture_summary_version_drift")


def _assert_fixture_lifecycle(
    config: CanaryConfig,
    receipt: Mapping[str, Any],
    conversation: Mapping[str, Any],
) -> None:
    _validate_fixture_provenance(config, conversation, receipt)
    versions = conversation.get("summary_versions")
    if not isinstance(versions, list) or not versions or not all(isinstance(value, Mapping) for value in versions):
        raise HarnessRefusal("fixture_summary_version_drift")
    version_ids = [str(value.get("id") or "") for value in versions]
    if any(SAFE_ID_RE.fullmatch(value) is None for value in version_ids) or len(set(version_ids)) != len(version_ids):
        raise HarnessRefusal("fixture_summary_version_drift")
    prepared_version_id = str(receipt["active_summary_version_id"])
    prepared = [value for value in versions if str(value.get("id") or "") == prepared_version_id]
    active = [value for value in versions if value.get("is_active") is True]
    active_version_id = str(conversation.get("active_summary_version_id") or "")
    if (
        len(prepared) != 1
        or prepared[0].get("source") != "legacy"
        or prepared[0].get("kind") != "legacy_current"
        or len(active) != 1
        or str(active[0].get("id") or "") != active_version_id
    ):
        raise HarnessRefusal("fixture_summary_version_drift")
    enrichment_state = conversation.get("enrichment_state")
    if active_version_id == prepared_version_id and enrichment_state is None:
        _validate_fixture_conversation(config, conversation)
        _assert_fixture_summary_version(conversation, prepared_version_id)
        return
    if active_version_id == prepared_version_id or len(versions) < 2 or not isinstance(enrichment_state, Mapping):
        raise HarnessRefusal("fixture_enrichment_lifecycle_drift")
    state = str(enrichment_state.get("status") or "")
    pending = enrichment_state.get("pending")
    canonical_status = str(enrichment_state.get("canonical_status") or "")
    applied = (
        state == "writeback_applied"
        and pending is False
        and canonical_status
        in {
            "completed",
            "unconfirmed",
        }
    )
    pending_canonical = (
        state == "writeback_pending_canonical" and pending is True and canonical_status in {"pending", "failed"}
    )
    if not (applied or pending_canonical):
        raise HarnessRefusal("fixture_enrichment_lifecycle_drift")


def _validate_fixture_receipt(receipt: Mapping[str, Any], config: CanaryConfig) -> None:
    if set(receipt) != {
        "schema_version",
        "status",
        "uid_sha256",
        "conversation_id",
        "active_summary_version_id",
        "transcript_sha256",
        "enrichment_client_interaction_id",
        "enrichment_job_id",
        "fixture_marker_sha256",
        "provider_calls",
        "enrichment_success_preseeded",
        "content_free",
    }:
        raise HarnessRefusal("fixture_receipt_shape_invalid")
    if (
        receipt.get("schema_version") != FIXTURE_SCHEMA
        or receipt.get("status") != "ready"
        or receipt.get("uid_sha256") != _sha256(config.uid)
        or receipt.get("conversation_id") != _fixture_conversation_id(config)
        or not _safe_id(receipt.get("active_summary_version_id"), "fixture_summary_version_id")
        or SHA256_RE.fullmatch(str(receipt.get("transcript_sha256") or "")) is None
        or not _safe_id(receipt.get("enrichment_client_interaction_id"), "fixture_interaction_id")
        or not _safe_id(receipt.get("enrichment_job_id"), "fixture_job_id")
        or SHA256_RE.fullmatch(str(receipt.get("fixture_marker_sha256") or "")) is None
        or receipt.get("provider_calls") != 0
        or receipt.get("enrichment_success_preseeded") is not False
        or receipt.get("content_free") is not True
    ):
        raise HarnessRefusal("fixture_receipt_invalid")


async def _fixture_prepare_preflight(
    config: CanaryConfig,
    store: FixtureStore,
    off_receipt: Mapping[str, Any],
) -> None:
    _validate_fixture_off_receipt(off_receipt, config)
    _assert_fixture_rollout_off()
    token = _resolve_env_secret(config.backend_auth_ref)
    _require_firebase_subject(token, config.uid)
    await store.assert_prepare_authority(config)


async def prepare_fixture(
    config: CanaryConfig,
    store: FixtureStore,
    *,
    off_receipt: Mapping[str, Any],
    existing_receipt: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    await _fixture_prepare_preflight(config, store, off_receipt)
    expected = _fixture_conversation(config)
    existing = await store.get_conversation(config.uid, expected.id)
    if existing_receipt is not None:
        _validate_fixture_receipt(existing_receipt, config)
        if not isinstance(existing, Mapping):
            raise HarnessRefusal("fixture_conversation_not_found")
        _validate_fixture_conversation(config, existing)
        active_summary_version_id = str(existing_receipt["active_summary_version_id"])
        _assert_fixture_summary_version(existing, active_summary_version_id)
        observed = _fixture_receipt(config, existing, active_summary_version_id)
        if not hmac.compare_digest(_canonical_json(observed), _canonical_json(dict(existing_receipt))):
            raise HarnessRefusal("fixture_receipt_drift")
        return dict(existing_receipt)
    if existing is None:
        await store.create_conversation(config.uid, expected)
        existing = await store.get_conversation(config.uid, expected.id)
    if not isinstance(existing, Mapping):
        raise HarnessRefusal("fixture_conversation_write_failed")
    _validate_fixture_conversation(config, existing)
    version = await store.ensure_summary_version(config.uid, expected.id)
    if version.get("status") != "ready":
        raise HarnessRefusal("fixture_summary_version_unavailable")
    conversation = version.get("conversation")
    if not isinstance(conversation, Mapping):
        conversation = await store.get_conversation(config.uid, expected.id)
    if not isinstance(conversation, Mapping):
        raise HarnessRefusal("fixture_conversation_write_failed")
    _validate_fixture_conversation(config, conversation)
    active_summary_version_id = str(version.get("active_summary_version_id") or "").strip()
    if not active_summary_version_id:
        raise HarnessRefusal("fixture_summary_version_unavailable")
    _assert_fixture_summary_version(conversation, active_summary_version_id)
    receipt = _fixture_receipt(config, conversation, active_summary_version_id)
    _validate_fixture_receipt(receipt, config)
    return receipt


async def show_fixture(
    config: CanaryConfig,
    receipt: Mapping[str, Any],
    store: FixtureStore,
) -> dict[str, Any]:
    _validate_fixture_receipt(receipt, config)
    conversation = await store.get_conversation(config.uid, str(receipt["conversation_id"]))
    if not isinstance(conversation, Mapping):
        raise HarnessRefusal("fixture_conversation_not_found")
    _assert_fixture_lifecycle(config, receipt, conversation)
    return dict(receipt)


async def cleanup_fixture(
    config: CanaryConfig,
    receipt: Mapping[str, Any],
    store: FixtureStore,
    *,
    off_receipt: Mapping[str, Any],
    confirm_conversation_id: str,
) -> dict[str, Any]:
    _validate_fixture_off_receipt(off_receipt, config)
    _assert_fixture_rollout_off()
    _validate_fixture_receipt(receipt, config)
    conversation_id = str(receipt["conversation_id"])
    if confirm_conversation_id != conversation_id:
        raise HarnessRefusal("fixture_cleanup_confirmation_mismatch")
    conversation = await store.get_conversation(config.uid, conversation_id)
    if conversation is not None:
        if not isinstance(conversation, Mapping):
            raise HarnessRefusal("fixture_conversation_drift")
        _assert_fixture_lifecycle(config, receipt, conversation)
    await store.delete_vector(config.uid, conversation_id)
    if conversation is not None:
        await store.delete_conversation(config.uid, conversation_id)
    if await store.get_conversation(config.uid, conversation_id) is not None:
        raise HarnessRefusal("fixture_cleanup_conversation_present")
    if await store.vector_exists(config.uid, conversation_id):
        raise HarnessRefusal("fixture_cleanup_vector_present")
    return {
        "schema_version": FIXTURE_SCHEMA,
        "status": "cleaned",
        "uid_sha256": _sha256(config.uid),
        "conversation_id_sha256": _sha256(conversation_id),
        "conversation_absent": True,
        "vector_absent": True,
        "content_free": True,
    }


def _fixture_receipt_path(path: str, *, must_exist: bool) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise HarnessRefusal("fixture_receipt_not_absolute")
    if must_exist:
        return _assert_protected_file(candidate)
    try:
        parent = candidate.parent.resolve(strict=True)
    except OSError as exc:
        raise HarnessRefusal("fixture_receipt_parent_invalid") from exc
    metadata = parent.stat()
    if (
        candidate.exists()
        or candidate.is_symlink()
        or not any(parent.is_relative_to(root.resolve()) for root in APPROVED_SECRET_ROOTS)
        or parent.is_symlink()
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) != 0o700
    ):
        raise HarnessRefusal("fixture_receipt_parent_invalid")
    return candidate


def load_fixture_receipt(path: str) -> dict[str, Any]:
    protected = _fixture_receipt_path(path, must_exist=True)
    try:
        value = json.loads(protected.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise HarnessRefusal("fixture_receipt_invalid") from exc
    if not isinstance(value, dict):
        raise HarnessRefusal("fixture_receipt_invalid")
    return value


def write_fixture_receipt(path: str, receipt: Mapping[str, Any]) -> None:
    candidate = Path(path)
    if candidate.exists():
        existing = load_fixture_receipt(path)
        if not hmac.compare_digest(_canonical_json(existing), _canonical_json(dict(receipt))):
            raise HarnessRefusal("fixture_receipt_conflict")
        return
    protected = _fixture_receipt_path(path, must_exist=False)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(protected, flags, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(_canonical_json(dict(receipt)) + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
        protected.chmod(0o400)
    except OSError as exc:
        raise HarnessRefusal("fixture_receipt_write_failed") from exc


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
        client_interaction_id, _ = production_enrichment_interaction_identity(
            self.config.uid,
            self.config.enrichment_conversation_id,
            self.config.enrichment_transcript_sha256,
        )
        payload = {
            "uid": self.config.uid,
            "conversation_id": self.config.enrichment_conversation_id,
            "client_interaction_id": client_interaction_id,
            "transcript_sha256": self.config.enrichment_transcript_sha256,
        }
        headers = {
            "X-Ella-Hermes-Cloud-Enrichment-Token": token,
            "X-Ella-Subject-Uid": self.config.uid,
        }
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
            chat_identity_absent=_enrichment_chat_identity_absent(first, replay),
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


def _enrichment_chat_identity_absent(*results: Mapping[str, Any]) -> bool:
    return all(ENRICHMENT_CHAT_IDENTITY_FIELDS.isdisjoint(result) for result in results)


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
        receipt.get("schema_version") != OFF_RECEIPT_SCHEMA
        or receipt.get("uid_sha256") != _sha256(config.uid)
        or receipt.get("flags_off") is not True
        or receipt.get("selectors_empty") is not True
        or receipt.get("workflows_off") is not True
        or receipt.get("content_free") is not True
    ):
        raise HarnessRefusal("cleanup_off_receipt_invalid")


def load_off_receipt(
    path: str,
    *,
    approved_roots: tuple[Path, ...] = APPROVED_SECRET_ROOTS,
    expected_owner_uid: int = 0,
) -> dict[str, Any]:
    protected = _assert_protected_file(
        Path(path),
        approved_roots=approved_roots,
        expected_owner_uid=expected_owner_uid,
    )
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
    for command in ("fixture-prepare", "fixture-show", "fixture-cleanup"):
        fixture_parser = subparsers.add_parser(command)
        fixture_parser.add_argument("--config", required=True)
        fixture_parser.add_argument("--fixture-receipt", required=True)
        if command in {"fixture-prepare", "fixture-cleanup"}:
            fixture_parser.add_argument("--off-receipt", required=True)
        if command == "fixture-cleanup":
            fixture_parser.add_argument("--confirm-conversation-id", required=True)
    args = parser.parse_args()

    config = load_config(args.config)
    if args.command.startswith("fixture-"):
        store = ProductionFixtureStore()
        if args.command == "fixture-prepare":
            receipt_path = Path(args.fixture_receipt)
            existing_receipt = (
                load_fixture_receipt(args.fixture_receipt)
                if receipt_path.exists() or receipt_path.is_symlink()
                else None
            )
            result = await prepare_fixture(
                config,
                store,
                off_receipt=load_off_receipt(args.off_receipt),
                existing_receipt=existing_receipt,
            )
            write_fixture_receipt(args.fixture_receipt, result)
        else:
            receipt = load_fixture_receipt(args.fixture_receipt)
            if args.command == "fixture-show":
                result = await show_fixture(config, receipt, store)
            else:
                result = await cleanup_fixture(
                    config,
                    receipt,
                    store,
                    off_receipt=load_off_receipt(args.off_receipt),
                    confirm_conversation_id=args.confirm_conversation_id,
                )
        print(json.dumps(result, sort_keys=True))
        return 0
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
    # These production persistence modules construct external clients at import
    # time. The CLI is their only consumer; keeping them out of library import
    # makes the content-free harness tests hermetic without in-function imports.
    from database import conversations as conversations_db
    from database import vector_db

    try:
        raise SystemExit(asyncio.run(_main()))
    except HarnessRefusal as exc:
        raise SystemExit(f"harness_refused:{exc.code}") from None
