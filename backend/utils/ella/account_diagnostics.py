"""Shared, versioned contracts for content-free diagnostic evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import string
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from database.account_diagnostics import account_binding_fingerprint

DIAGNOSTIC_EVENT_SCHEMA_VERSION = "ella.diagnostic_event.v1"
DIAGNOSTIC_PROJECTION_SCHEMA_VERSION = "ella.account_state_projection.v1"
DIAGNOSTIC_STALE_AFTER = timedelta(minutes=10)
MAX_EVENT_BYTES = 4096
MAX_EVENTS_PER_BATCH = 100

_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SAFE_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_FINGERPRINT_RE = re.compile(r"^[a-f0-9]{64}$")
_SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:+/-]{0,127}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,127}$")
_SUPPORT_CODE_RE = re.compile(r"^ELLA-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}$")
_SAFE_COUNTER_NAMES = frozenset({"frames", "bytes", "retry_number", "rssi_bucket", "queue_age_seconds"})


class DiagnosticLayer(str, Enum):
    account_binding = "account_binding"
    ble_transport = "ble_transport"
    physical_audio = "physical_audio"
    server_capture = "server_capture"
    publication = "publication"
    presentation = "presentation"


DIAGNOSTIC_LAYER_ORDER = tuple(DiagnosticLayer)

DIAGNOSTIC_EVENT_REGISTRY: dict[str, frozenset[DiagnosticLayer]] = {
    "account_bound": frozenset({DiagnosticLayer.account_binding}),
    "capture_consent_current": frozenset({DiagnosticLayer.account_binding}),
    "capture_authority_current": frozenset({DiagnosticLayer.account_binding}),
    "diagnostic_session_started": frozenset({DiagnosticLayer.ble_transport}),
    "capture_attempt_started": frozenset({DiagnosticLayer.ble_transport}),
    "remembered_device_resolved": frozenset({DiagnosticLayer.ble_transport}),
    "peripheral_connect_started": frozenset({DiagnosticLayer.ble_transport}),
    "peripheral_connected": frozenset({DiagnosticLayer.ble_transport}),
    "peripheral_connect_failed": frozenset({DiagnosticLayer.ble_transport}),
    "notifications_subscribed": frozenset({DiagnosticLayer.ble_transport}),
    "notification_subscription_failed": frozenset({DiagnosticLayer.ble_transport}),
    "audio_subscription_open": frozenset({DiagnosticLayer.physical_audio}),
    "audio_first_frame": frozenset({DiagnosticLayer.physical_audio}),
    "audio_frames_advancing": frozenset({DiagnosticLayer.physical_audio}),
    "audio_frames_stalled": frozenset({DiagnosticLayer.physical_audio}),
    "websocket_authenticated": frozenset({DiagnosticLayer.server_capture}),
    "capture_protocol_ready": frozenset({DiagnosticLayer.server_capture}),
    "capture_first_frame_accepted": frozenset({DiagnosticLayer.server_capture}),
    "capture_drain_confirmed": frozenset({DiagnosticLayer.server_capture}),
    "capture_finalization_requested": frozenset({DiagnosticLayer.server_capture}),
    "capture_finalized": frozenset({DiagnosticLayer.publication}),
    "conversation_published": frozenset({DiagnosticLayer.publication}),
    "memory_visible": frozenset({DiagnosticLayer.presentation}),
    "artwork_visible": frozenset({DiagnosticLayer.presentation}),
}


class DiagnosticOutcome(str, Enum):
    started = "started"
    succeeded = "succeeded"
    failed = "failed"
    cancelled = "cancelled"
    unknown = "unknown"


class DiagnosticRetryClass(str, Enum):
    never = "never"
    user_action = "user_action"
    bounded_automatic = "bounded_automatic"
    operator_only = "operator_only"


class DiagnosticFailureCode(str, Enum):
    ble_not_powered = "ble_not_powered"
    ble_permission_denied = "ble_permission_denied"
    remembered_device_not_resolved = "remembered_device_not_resolved"
    peripheral_connect_timeout = "peripheral_connect_timeout"
    required_service_missing = "required_service_missing"
    notification_subscription_failed = "notification_subscription_failed"
    audio_first_frame_timeout = "audio_first_frame_timeout"
    audio_frames_stalled = "audio_frames_stalled"
    websocket_auth_failed = "websocket_auth_failed"
    capture_authority_conflict = "capture_authority_conflict"
    capture_ready_timeout = "capture_ready_timeout"
    capture_first_frame_rejected = "capture_first_frame_rejected"
    capture_drain_ambiguous = "capture_drain_ambiguous"
    finalization_timeout = "finalization_timeout"
    account_authority_changed = "account_authority_changed"
    client_backend_revision_mismatch = "client_backend_revision_mismatch"


FAILURE_REGISTRY: dict[DiagnosticFailureCode, tuple[DiagnosticLayer, DiagnosticRetryClass]] = {
    DiagnosticFailureCode.ble_not_powered: (DiagnosticLayer.ble_transport, DiagnosticRetryClass.user_action),
    DiagnosticFailureCode.ble_permission_denied: (DiagnosticLayer.ble_transport, DiagnosticRetryClass.user_action),
    DiagnosticFailureCode.remembered_device_not_resolved: (
        DiagnosticLayer.ble_transport,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.peripheral_connect_timeout: (
        DiagnosticLayer.ble_transport,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.required_service_missing: (DiagnosticLayer.ble_transport, DiagnosticRetryClass.never),
    DiagnosticFailureCode.notification_subscription_failed: (
        DiagnosticLayer.ble_transport,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.audio_first_frame_timeout: (
        DiagnosticLayer.physical_audio,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.audio_frames_stalled: (
        DiagnosticLayer.physical_audio,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.websocket_auth_failed: (DiagnosticLayer.server_capture, DiagnosticRetryClass.user_action),
    DiagnosticFailureCode.capture_authority_conflict: (
        DiagnosticLayer.server_capture,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.capture_ready_timeout: (
        DiagnosticLayer.server_capture,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.capture_first_frame_rejected: (
        DiagnosticLayer.server_capture,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.capture_drain_ambiguous: (
        DiagnosticLayer.publication,
        DiagnosticRetryClass.operator_only,
    ),
    DiagnosticFailureCode.finalization_timeout: (
        DiagnosticLayer.publication,
        DiagnosticRetryClass.bounded_automatic,
    ),
    DiagnosticFailureCode.account_authority_changed: (
        DiagnosticLayer.account_binding,
        DiagnosticRetryClass.user_action,
    ),
    DiagnosticFailureCode.client_backend_revision_mismatch: (
        DiagnosticLayer.account_binding,
        DiagnosticRetryClass.user_action,
    ),
}


class DiagnosticEventV1(BaseModel):
    """Strict allowlist shared with the iOS diagnostic spine."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["ella.diagnostic_event.v1"] = DIAGNOSTIC_EVENT_SCHEMA_VERSION
    event_id: str = Field(min_length=1, max_length=128)
    diagnostic_session_id: str = Field(min_length=1, max_length=128)
    capture_attempt_id: str = Field(min_length=1, max_length=128)
    account_binding_fingerprint: str = Field(min_length=64, max_length=64)
    authority_generation: int = Field(ge=0, le=9_223_372_036_854_775_807)
    source_revision: str = Field(min_length=1, max_length=128)
    layer: DiagnosticLayer
    event_name: str = Field(min_length=1, max_length=64)
    outcome: DiagnosticOutcome
    retry_class: DiagnosticRetryClass
    client_sequence: int = Field(ge=0, le=9_223_372_036_854_775_807)
    client_monotonic_ms: int = Field(ge=0, le=9_223_372_036_854_775_807)
    client_utc_time: datetime
    opaque_resource_id: str | None = Field(default=None, max_length=128)
    firmware: str | None = Field(default=None, max_length=128)
    codec: str | None = Field(default=None, max_length=128)
    stable_failure_code: DiagnosticFailureCode | None = None
    expected_next_event: str | None = Field(default=None, max_length=64)
    deadline_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    safe_counters: dict[str, int] = Field(default_factory=dict)
    projection_revision: int | None = Field(default=None, ge=0)
    action_revision: int | None = Field(default=None, ge=0)

    @field_validator("event_id", "diagnostic_session_id", "capture_attempt_id")
    @classmethod
    def validate_opaque_id(cls, value: str) -> str:
        if _OPAQUE_ID_RE.fullmatch(value) is None:
            raise ValueError("must be an opaque bounded identifier")
        return value

    @field_validator("account_binding_fingerprint")
    @classmethod
    def validate_fingerprint(cls, value: str) -> str:
        if _FINGERPRINT_RE.fullmatch(value) is None:
            raise ValueError("must be a lowercase SHA-256 fingerprint")
        return value

    @field_validator("source_revision")
    @classmethod
    def validate_source_revision(cls, value: str) -> str:
        if _REVISION_RE.fullmatch(value) is None:
            raise ValueError("must be a safe bounded revision")
        return value

    @field_validator("event_name", "expected_next_event")
    @classmethod
    def validate_event_name(cls, value: str | None) -> str | None:
        if value is not None and _SAFE_NAME_RE.fullmatch(value) is None:
            raise ValueError("must use the stable event-name wire format")
        return value

    @field_validator("opaque_resource_id", "firmware", "codec")
    @classmethod
    def validate_safe_value(cls, value: str | None) -> str | None:
        if value is not None and (
            _SAFE_VALUE_RE.fullmatch(value) is None or "://" in value or "?" in value or "@" in value
        ):
            raise ValueError("must not contain raw identifying or content data")
        return value

    @field_validator("client_utc_time")
    @classmethod
    def validate_client_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("must include a timezone")
        return value.astimezone(timezone.utc)

    @field_validator("safe_counters")
    @classmethod
    def validate_safe_counters(cls, value: dict[str, int]) -> dict[str, int]:
        if set(value) - _SAFE_COUNTER_NAMES:
            raise ValueError("contains a non-allowlisted counter")
        if any(isinstance(item, bool) or item < 0 or item > 1_000_000_000_000 for item in value.values()):
            raise ValueError("contains an invalid counter value")
        return value

    @model_validator(mode="after")
    def validate_failure_contract(self) -> "DiagnosticEventV1":
        registered_layers = DIAGNOSTIC_EVENT_REGISTRY.get(self.event_name)
        if registered_layers is None or self.layer not in registered_layers:
            raise ValueError("event name does not match the registered diagnostic layer")
        if self.expected_next_event is not None and self.expected_next_event not in DIAGNOSTIC_EVENT_REGISTRY:
            raise ValueError("expected_next_event is not registered")
        if self.outcome == DiagnosticOutcome.failed and self.stable_failure_code is None:
            raise ValueError("failed events require stable_failure_code")
        if self.stable_failure_code is not None:
            expected_layer, expected_retry = FAILURE_REGISTRY[self.stable_failure_code]
            if self.layer != expected_layer or self.retry_class != expected_retry:
                raise ValueError("failure code does not match its registered layer and retry class")
        encoded = self.model_dump_json(exclude_none=True).encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise ValueError("event exceeds the maximum encoded size")
        return self


class DiagnosticEventBatchV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    events: list[DiagnosticEventV1] = Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)

    @model_validator(mode="after")
    def validate_single_authority(self) -> "DiagnosticEventBatchV1":
        fingerprints = {event.account_binding_fingerprint for event in self.events}
        if len(fingerprints) != 1:
            raise ValueError("one batch must belong to exactly one account authority")
        event_ids = {event.event_id for event in self.events}
        coordinates = {
            (event.diagnostic_session_id, event.capture_attempt_id, event.client_sequence) for event in self.events
        }
        if len(event_ids) != len(self.events) or len(coordinates) != len(self.events):
            raise ValueError("one batch must not contain duplicate event identities or sequences")
        return self


@dataclass(frozen=True)
class StoredDiagnosticEvent:
    event: DiagnosticEventV1
    server_received_at: datetime


class DiagnosticLayerProjection(BaseModel):
    layer: DiagnosticLayer
    state: Literal["unknown", "in_progress", "succeeded", "failed", "cancelled"]
    last_event_name: str | None = None
    last_outcome: DiagnosticOutcome | None = None
    stable_failure_code: DiagnosticFailureCode | None = None
    observed_event_count: int = Field(ge=0)


class AccountStateProjectionV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["ella.account_state_projection.v1"] = DIAGNOSTIC_PROJECTION_SCHEMA_VERSION
    diagnostic_session_id: str
    capture_attempt_id: str | None
    account_binding_fingerprint: str | None
    status: Literal["no_evidence", "incomplete", "failed", "healthy"]
    complete: bool
    stale: bool
    first_unresolved_layer: DiagnosticLayer | None
    stable_failure_code: DiagnosticFailureCode | None
    observed_event_count: int = Field(ge=0)
    last_server_observed_at: datetime | None
    layers: list[DiagnosticLayerProjection]


def project_account_state(
    diagnostic_session_id: str,
    evidence: list[StoredDiagnosticEvent],
    *,
    now: datetime | None = None,
) -> AccountStateProjectionV1:
    """Build a disposable view; missing evidence remains unknown, never false."""
    observed_now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    session_evidence = [item for item in evidence if item.event.diagnostic_session_id == diagnostic_session_id]
    if not session_evidence:
        return AccountStateProjectionV1(
            diagnostic_session_id=diagnostic_session_id,
            capture_attempt_id=None,
            account_binding_fingerprint=None,
            status="no_evidence",
            complete=False,
            stale=False,
            first_unresolved_layer=DiagnosticLayer.account_binding,
            stable_failure_code=None,
            observed_event_count=0,
            last_server_observed_at=None,
            layers=[
                DiagnosticLayerProjection(layer=layer, state="unknown", observed_event_count=0)
                for layer in DIAGNOSTIC_LAYER_ORDER
            ],
        )

    attempt_starts = [item for item in session_evidence if item.event.event_name == "capture_attempt_started"]
    latest_attempt = max(
        attempt_starts or session_evidence,
        key=lambda item: (
            item.event.client_monotonic_ms,
            item.event.client_utc_time,
            item.event.client_sequence,
            item.event.event_id,
        ),
    ).event.capture_attempt_id
    attempt_evidence = [item for item in session_evidence if item.event.capture_attempt_id == latest_attempt]
    ordered = sorted(
        attempt_evidence,
        key=lambda item: (item.event.client_sequence, item.event.client_monotonic_ms, item.server_received_at),
    )
    layer_projections: list[DiagnosticLayerProjection] = []
    for layer in DIAGNOSTIC_LAYER_ORDER:
        matches = [item for item in ordered if item.event.layer == layer]
        if not matches:
            layer_projections.append(DiagnosticLayerProjection(layer=layer, state="unknown", observed_event_count=0))
            continue
        last = matches[-1].event
        state = {
            DiagnosticOutcome.started: "in_progress",
            DiagnosticOutcome.succeeded: "succeeded",
            DiagnosticOutcome.failed: "failed",
            DiagnosticOutcome.cancelled: "cancelled",
            DiagnosticOutcome.unknown: "unknown",
        }[last.outcome]
        layer_projections.append(
            DiagnosticLayerProjection(
                layer=layer,
                state=state,
                last_event_name=last.event_name,
                last_outcome=last.outcome,
                stable_failure_code=last.stable_failure_code,
                observed_event_count=len(matches),
            )
        )

    first_unresolved = next((item for item in layer_projections if item.state != "succeeded"), None)
    failure = next((item for item in layer_projections if item.state == "failed"), None)
    last_received = max(item.server_received_at for item in ordered).astimezone(timezone.utc)
    stale = observed_now - last_received > DIAGNOSTIC_STALE_AFTER
    complete = first_unresolved is None
    status_value: Literal["no_evidence", "incomplete", "failed", "healthy"]
    if failure is not None:
        status_value = "failed"
    elif complete:
        status_value = "healthy"
    else:
        status_value = "incomplete"
    return AccountStateProjectionV1(
        diagnostic_session_id=diagnostic_session_id,
        capture_attempt_id=latest_attempt,
        account_binding_fingerprint=ordered[-1].event.account_binding_fingerprint,
        status=status_value,
        complete=complete,
        stale=stale,
        first_unresolved_layer=first_unresolved.layer if first_unresolved else None,
        stable_failure_code=failure.stable_failure_code if failure else None,
        observed_event_count=len(ordered),
        last_server_observed_at=last_received,
        layers=layer_projections,
    )


def generate_support_code() -> str:
    alphabet = string.ascii_uppercase.replace("I", "").replace("L", "").replace("O", "") + "23456789"
    groups = ["".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(3)]
    return "ELLA-" + "-".join(groups)


def support_code_hash(code: str, *, hmac_key: str) -> str:
    normalized = code.strip().upper()
    if _SUPPORT_CODE_RE.fullmatch(normalized) is None:
        raise ValueError("invalid support code")
    if len(hmac_key) < 32:
        raise ValueError("support-code HMAC key must contain at least 32 characters")
    return hmac.new(hmac_key.encode("utf-8"), normalized.encode("ascii"), hashlib.sha256).hexdigest()


def event_from_record(record: Any) -> StoredDiagnosticEvent:
    if isinstance(record, StoredDiagnosticEvent):
        return record
    payload = record["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return StoredDiagnosticEvent(
        event=DiagnosticEventV1.model_validate(payload),
        server_received_at=record["server_received_at"],
    )
