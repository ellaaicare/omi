from __future__ import annotations

import asyncio
import functools
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

import pytest
from aiohttp.test_utils import TestClient, TestServer

BRIDGE_ROOT = Path(__file__).resolve().parents[2] / "sidecars" / "imessage_photon_bridge"
sys.path.insert(0, str(BRIDGE_ROOT))

from bridge import (  # noqa: E402
    BridgeConfig,
    BridgeError,
    BridgeJournal,
    ImessagePhotonBridge,
    ProviderRegistration,
    BridgeHttpServer,
    SingletonLease,
    _is_provider_stream_healthy,
    _normalize_provider_event,
    send_provider_text_once,
)

PROJECT_ID = "ed929a1e-9d91-4be2-a4a8-7d9571dab702"
PHONE_A = "+15550000001"
PHONE_B = "+15550000002"
LINE_A = "+15550000101"
LINE_B = "+15550000102"
TOKEN_A = "a" * 32
TOKEN_B = "b" * 32


def async_test(function: Callable[..., Awaitable[None]]) -> Callable[..., None]:
    @functools.wraps(function)
    def run(*args: Any, **kwargs: Any) -> None:
        asyncio.run(function(*args, **kwargs))

    return run


def _config(tmp_path: Path, **overrides: Any) -> BridgeConfig:
    values = {
        "backend_base_url": "https://api.ella-ai-care.com",
        "registrar_bind": "127.0.0.1",
        "registrar_port": 8796,
        "state_directory": tmp_path / "state",
        "project_id": PROJECT_ID,
        "transport_token": TOKEN_A,
        "registrar_token": TOKEN_B,
        "heartbeat_seconds": 0.01,
        "backend_timeout_seconds": 90.0,
    }
    values.update(overrides)
    return BridgeConfig(**values)


def _event(message_id: str, sender: str, text: str = "hello") -> dict[str, Any]:
    return {
        "messageId": message_id,
        "platform": "iMessage",
        "space": {"id": f"any;-;{sender}", "type": "dm", "phone": "shared"},
        "sender": {"id": sender},
        "content": {"type": "text", "text": text},
        "timestamp": "2026-09-18T10:00:00Z",
    }


def _ready_registration(
    journal: BridgeJournal,
    *,
    phone: str = PHONE_A,
    line: str = LINE_A,
    provider_user_id: str = "provider-user-a",
) -> dict[str, Any]:
    request_id = str(uuid.uuid4())
    journal.begin_registration(request_id, PROJECT_ID, phone)
    journal.mark_registration_attempt_started(request_id)
    return journal.complete_registration(
        request_id,
        ProviderRegistration(
            project_id=PROJECT_ID,
            registration_id=f"{PROJECT_ID}:{provider_user_id}",
            provider_user_id=provider_user_id,
            handset_e164=phone,
            assigned_destination=line,
        ),
    )


class FakeProvider:
    def __init__(self) -> None:
        self.users: list[dict[str, Any]] = []
        self.register_calls = 0
        self.send_calls: list[tuple[str, str]] = []
        self.send_result: Optional[str] = "provider-outbound-1"
        self.is_healthy = True
        self.handler: Optional[Callable[[dict[str, Any]], Awaitable[None]]] = None
        self.fatal_handler: Optional[Callable[[], Awaitable[None]]] = None
        self.order: list[str] = []

    async def connect(
        self,
        handler: Callable[[dict[str, Any]], Awaitable[None]],
        fatal_handler: Callable[[], Awaitable[None]],
    ) -> bool:
        self.handler = handler
        self.fatal_handler = fatal_handler
        return True

    async def disconnect(self) -> None:
        self.handler = None
        self.fatal_handler = None

    async def healthy(self) -> bool:
        return self.is_healthy

    async def list_users(self) -> list[dict[str, Any]]:
        return list(self.users)

    async def register_user(self, handset_e164: str) -> dict[str, Any]:
        self.register_calls += 1
        user = {
            "id": f"provider-user-{self.register_calls}",
            "phoneNumber": handset_e164,
            "assignedPhoneNumber": LINE_A,
        }
        self.users.append(user)
        return user

    async def send_text(self, handset_e164: str, text: str) -> Optional[str]:
        self.order.append("provider_send")
        self.send_calls.append((handset_e164, text))
        return self.send_result


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.order: list[str] = []
        self.inbound_result = {
            "status": "delivered",
            "model_invoked": False,
        }
        self.start_result: Optional[dict[str, Any]] = None
        self.inbound_entered = asyncio.Event()
        self.release_inbound: Optional[asyncio.Event] = None
        self.delivery_start_error: Optional[BridgeError] = None
        self.delivery_ack_error: Optional[BridgeError] = None
        self.heartbeat_error: Optional[BridgeError] = None
        self.inbound_errors: list[BridgeError] = []

    async def proof(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("proof", payload))
        return {"status": "accepted", "authority_generation": 1}

    async def heartbeat(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("heartbeat", payload))
        if self.heartbeat_error is not None:
            raise self.heartbeat_error
        return {"status": "ready", "binding_generation": 1, "text_dm_only": True}

    async def inbound(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.order.append("inbound")
        self.calls.append(("inbound", payload))
        self.inbound_entered.set()
        if self.inbound_errors:
            raise self.inbound_errors.pop(0)
        if self.release_inbound is not None:
            await self.release_inbound.wait()
        return dict(self.inbound_result)

    async def delivery_start(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.order.append("delivery_start")
        self.calls.append(("delivery_start", payload))
        if self.delivery_start_error is not None:
            raise self.delivery_start_error
        assert self.start_result is not None
        return dict(self.start_result)

    async def delivery_ack(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.order.append("delivery_ack")
        self.calls.append(("delivery_ack", payload))
        if self.delivery_ack_error is not None:
            raise self.delivery_ack_error
        return {"status": "delivered", "receipt_id": payload["receipt_id"]}

    async def delivery_uncertain(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.order.append("delivery_uncertain")
        self.calls.append(("delivery_uncertain", payload))
        return {"status": "uncertain", "receipt_id": payload["receipt_id"], "retryable": False}

    async def deregister(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.calls.append(("deregister", payload))
        return {"status": "quarantined", "binding_generation": 2}


def _delivery_ids() -> tuple[str, str]:
    return str(uuid.uuid4()), str(uuid.uuid4())


def _bridge(tmp_path: Path) -> tuple[ImessagePhotonBridge, BridgeJournal, FakeProvider, FakeBackend]:
    config = _config(tmp_path)
    journal = BridgeJournal(config.state_directory, config.project_id)
    provider = FakeProvider()
    backend = FakeBackend()
    bridge = ImessagePhotonBridge(
        config=config,
        journal=journal,
        provider=provider,
        backend=backend,
        connection_id="connection-1",
    )
    return bridge, journal, provider, backend


def test_bridge_config_requires_https_or_exact_loopback_and_distinct_tokens(tmp_path: Path) -> None:
    with pytest.raises(BridgeError, match="bridge_backend_url_invalid"):
        _config(tmp_path, backend_base_url="http://example.com").validated()
    with pytest.raises(BridgeError, match="bridge_registrar_must_be_loopback"):
        _config(tmp_path, registrar_bind="0.0.0.0").validated()
    with pytest.raises(BridgeError, match="bridge_service_tokens_must_be_distinct"):
        _config(tmp_path, registrar_token=TOKEN_A).validated()
    assert _config(tmp_path, heartbeat_seconds=30.0).validated().project_id == PROJECT_ID


def test_provider_health_requires_exact_healthy_non_zombie_stream() -> None:
    assert _is_provider_stream_healthy({"stream": {"ok": True, "state": "healthy"}})
    assert not _is_provider_stream_healthy({"stream": {"ok": True, "state": "starting"}})
    assert not _is_provider_stream_healthy({"stream": {"ok": True, "state": "recovering"}})
    assert not _is_provider_stream_healthy(
        {"stream": {"ok": True, "state": "healthy", "staleness": {"zombieSuspected": True}}}
    )


def test_entrypoint_rejects_missing_transport_home_before_hermes_import(tmp_path: Path) -> None:
    environment = dict(os.environ)
    environment["HERMES_IMPORT_ROOT"] = str(tmp_path / "immutable-hermes")
    environment.pop("HERMES_HOME", None)
    result = subprocess.run(
        [sys.executable, str(BRIDGE_ROOT / "main.py"), "--help"],
        capture_output=True,
        text=True,
        check=False,
        env=environment,
    )
    assert result.returncode != 0
    assert result.stderr.strip() == "HERMES_HOME must name the isolated Ella transport home"
    assert "fallback" not in result.stderr.lower()


def test_bridge_runtime_dependency_manifest_is_pinned() -> None:
    assert (BRIDGE_ROOT / "requirements.txt").read_text(encoding="ascii") == "aiohttp==3.14.3\n"


def test_journal_and_singleton_are_owner_only_and_exclusive(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir(mode=0o700)
    first = SingletonLease(state / "bridge.lock")
    second = SingletonLease(state / "bridge.lock")
    first.acquire()
    with pytest.raises(BridgeError, match="bridge_singleton_already_running"):
        second.acquire()
    first.close()
    second.acquire()
    second.close()
    journal = BridgeJournal(state, PROJECT_ID)
    assert stat.S_IMODE(state.stat().st_mode) == 0o700
    assert stat.S_IMODE(journal.path.stat().st_mode) == 0o600
    journal.close()


def test_journal_refuses_reuse_by_a_different_provider_project(tmp_path: Path) -> None:
    state = tmp_path / "state"
    first = BridgeJournal(state, PROJECT_ID)
    first.close()
    with pytest.raises(BridgeError, match="bridge_state_project_mismatch"):
        BridgeJournal(state, "f01c81a2-6a51-4b56-928a-981a831e26af")


@async_test
async def test_registrar_is_idempotent_and_does_not_repeat_uncertain_create(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    request_id = str(uuid.uuid4())
    status_code, first = await bridge.registrar.register(request_id, PHONE_A)
    assert status_code == 201
    assert first["assigned_destination"] == LINE_A
    status_code, second = await bridge.registrar.register(request_id, PHONE_A)
    assert status_code == 200
    assert second == first
    assert provider.register_calls == 1

    other_phone = PHONE_B
    other_request = str(uuid.uuid4())
    journal.begin_registration(other_request, PROJECT_ID, other_phone)
    journal.mark_registration_attempt_started(other_request)
    with pytest.raises(BridgeError, match="bridge_provider_registration_pending"):
        await bridge.registrar.register(other_request, other_phone)
    with pytest.raises(BridgeError, match="bridge_provider_registration_pending"):
        await bridge.registrar.register(other_request, other_phone)
    assert provider.register_calls == 1
    journal.close()


@async_test
async def test_registrar_waits_for_assigned_destination_without_duplicate_provider_create(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    request_id = str(uuid.uuid4())
    provider.send_result = None
    provider.users.append(
        {
            "id": "provider-user-pending",
            "phoneNumber": PHONE_A,
            "assignedPhoneNumber": None,
        }
    )
    with pytest.raises(BridgeError, match="bridge_provider_registration_incomplete"):
        await bridge.registrar.register(request_id, PHONE_A)
    assert provider.register_calls == 0
    provider.users[0]["assignedPhoneNumber"] = LINE_A
    status_code, response = await bridge.registrar.register(request_id, PHONE_A)
    assert status_code == 201
    assert response["assigned_destination"] == LINE_A
    assert provider.register_calls == 0
    journal.close()


@async_test
async def test_deregistered_handset_can_reenroll_with_new_idempotency_key(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    first = _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    provider.users.append(
        {
            "id": first["provider_user_id"],
            "phoneNumber": PHONE_A,
            "assignedPhoneNumber": LINE_A,
        }
    )
    await bridge.deregister_all()

    replacement_request_id = str(uuid.uuid4())
    status_code, response = await bridge.registrar.register(replacement_request_id, PHONE_A)

    assert status_code == 201
    assert response["assigned_destination"] == LINE_A
    assert provider.register_calls == 0
    rows = journal.connection.execute(
        "SELECT provider_request_id, status, proof_accepted FROM registrations WHERE handset_e164 = ?",
        (PHONE_A,),
    ).fetchall()
    assert [tuple(row) for row in rows] == [(replacement_request_id, "ready", 0)]
    journal.close()


@async_test
async def test_registrar_http_requires_auth_and_returns_no_store(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    client = TestClient(TestServer(BridgeHttpServer(bridge).application()))
    await client.start_server()
    try:
        denied = await client.post(
            "/v1/registrations",
            headers={"Idempotency-Key": str(uuid.uuid4())},
            json={"channel": "imessage", "mode": "text_dm", "handset_e164": PHONE_A},
        )
        assert denied.status == 401
        assert denied.headers["Cache-Control"] == "no-store"
        assert provider.register_calls == 0

        accepted = await client.post(
            "/v1/registrations",
            headers={
                "Authorization": f"Bearer {TOKEN_B}",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={"channel": "imessage", "mode": "text_dm", "handset_e164": PHONE_A},
        )
        assert accepted.status == 201
        assert accepted.headers["Cache-Control"] == "no-store"
        assert set(await accepted.json()) == {"registration_id", "assigned_destination"}
        assert provider.register_calls == 1
    finally:
        await client.close()
        journal.close()


@async_test
async def test_shared_sentinel_maps_two_senders_to_distinct_contacts_without_owner_selector(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal, phone=PHONE_A, line=LINE_A, provider_user_id="user-a")
    _ready_registration(journal, phone=PHONE_B, line=LINE_B, provider_user_id="user-b")
    await bridge.accept_provider_event(_event("in-a", PHONE_A))
    await bridge.accept_provider_event(_event("in-b", PHONE_B))
    for message_id in (await bridge.queue.get(), await bridge.queue.get()):
        await bridge._process(message_id)
        bridge.queue.task_done()
    inbound_calls = [payload for name, payload in backend.calls if name == "inbound"]
    assert len(inbound_calls) == 2
    assert {payload["contact_identity"] for payload in inbound_calls} == {
        f"photon:{PROJECT_ID}:user-a:{PHONE_A}",
        f"photon:{PROJECT_ID}:user-b:{PHONE_B}",
    }
    assert {payload["line_identity"] for payload in inbound_calls} == {f"photon:{PROJECT_ID}:shared"}
    assert all("uid" not in payload and "profile" not in payload for payload in inbound_calls)
    journal.close()


@async_test
async def test_missing_mapping_group_attachment_and_non_shared_events_never_reach_backend(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    await bridge.accept_provider_event(_event("unknown", PHONE_B))
    group = _event("group", PHONE_A)
    group["space"] = {"id": "group-guid", "type": "group", "phone": "shared"}
    await bridge.accept_provider_event(group)
    attachment = _event("attachment", PHONE_A)
    attachment["content"] = {"type": "attachment"}
    await bridge.accept_provider_event(attachment)
    wrong_line = _event("wrong-line", PHONE_A)
    wrong_line["space"]["phone"] = LINE_A
    await bridge.accept_provider_event(wrong_line)
    assert bridge.queue.empty()
    assert backend.calls == []
    journal.close()


def test_changed_replay_payload_is_quarantined(tmp_path: Path) -> None:
    config = _config(tmp_path)
    journal = BridgeJournal(config.state_directory, config.project_id)
    first = _normalize_provider_event(PROJECT_ID, _event("same-id", PHONE_A, "first"))
    second = _normalize_provider_event(PROJECT_ID, _event("same-id", PHONE_A, "changed"))
    assert journal.record_inbound(first) == "new"
    with pytest.raises(BridgeError, match="bridge_inbound_replay_conflict"):
        journal.record_inbound(second)
    row = journal.connection.execute("SELECT status, event_json FROM inbound_events").fetchone()
    assert tuple(row) == ("quarantined", None)
    journal.close()


def test_normalized_event_discards_unrecognized_provider_fields_and_bounds_content() -> None:
    raw = _event("minimal-event", PHONE_A)
    raw["providerDebug"] = {"credential": "must-not-persist"}
    inbound = _normalize_provider_event(PROJECT_ID, raw)
    assert "providerDebug" not in inbound.raw_event
    assert set(inbound.raw_event) == {"project_id", "messageId", "platform", "space", "sender", "content", "timestamp"}
    too_large = _event("too-large", PHONE_A, "x" * 32_769)
    with pytest.raises(BridgeError, match="bridge_provider_event_invalid"):
        _normalize_provider_event(PROJECT_ID, too_large)


@async_test
async def test_six_digit_proof_uses_proof_route_and_never_invokes_model(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    await bridge.accept_provider_event(_event("proof-message", PHONE_A, "123456"))
    message_id = await bridge.queue.get()
    await bridge._process(message_id)
    assert [name for name, _payload in backend.calls] == ["proof"]
    registration = journal.registration_for_handset(PHONE_A)
    assert registration is not None and registration["proof_accepted"] == 1
    journal.close()


@async_test
async def test_six_digit_text_after_proof_uses_normal_inbound_route(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    await bridge.accept_provider_event(_event("ordinary-six-digits", PHONE_A, "123456"))
    await bridge._process(await bridge.queue.get())
    assert [name for name, _payload in backend.calls] == ["inbound"]
    journal.close()


@async_test
async def test_heartbeat_continues_while_backend_model_call_is_slow(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    backend.release_inbound = asyncio.Event()
    await bridge.start()
    try:
        await bridge.accept_provider_event(_event("slow-model", PHONE_A))
        await asyncio.wait_for(backend.inbound_entered.wait(), timeout=1)
        await asyncio.sleep(0.04)
        assert any(name == "heartbeat" for name, _payload in backend.calls)
        backend.release_inbound.set()
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
    finally:
        await bridge.stop()
        journal.close()
    assert provider.handler is None


@async_test
async def test_start_heartbeats_before_replaying_pending_inbound(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    journal.record_inbound(_normalize_provider_event(PROJECT_ID, _event("startup-replay", PHONE_A)))

    await bridge.start()
    try:
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
    finally:
        await bridge.stop()
        journal.close()

    call_names = [name for name, _payload in backend.calls]
    assert call_names[0] == "heartbeat"
    assert call_names.count("inbound") == 1
    assert call_names.index("heartbeat") < call_names.index("inbound")


@async_test
async def test_start_refuses_failed_initial_heartbeat_before_workers_and_replay(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    journal.record_inbound(_normalize_provider_event(PROJECT_ID, _event("blocked-replay", PHONE_A)))
    backend.heartbeat_error = BridgeError("bridge_backend_transport_failed", retryable=True)

    with pytest.raises(BridgeError, match="bridge_backend_transport_failed"):
        await bridge.start()

    assert bridge.provider_ready is False
    assert bridge.worker_task is None
    assert bridge.heartbeat_task is None
    assert provider.handler is None
    assert [name for name, _payload in backend.calls] == ["heartbeat"]
    assert journal.pending_inbound()[0]["status"] == "pending"
    journal.close()


@async_test
async def test_worker_retries_retryable_inbound_failure_without_restart(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    bridge.inbound_retry_delays = (0.0,)
    backend.inbound_errors = [BridgeError("bridge_backend_transport_failed", retryable=True)]

    await bridge.start()
    try:
        await bridge.accept_provider_event(_event("retry-in-process", PHONE_A))
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
    finally:
        await bridge.stop()

    assert [name for name, _payload in backend.calls].count("inbound") == 2
    row = journal.connection.execute(
        "SELECT status, event_json FROM inbound_events WHERE provider_message_id = 'retry-in-process'"
    ).fetchone()
    assert tuple(row) == ("terminal", None)
    journal.close()


@async_test
async def test_worker_bounds_retryable_inbound_failures_then_quarantines(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    bridge.inbound_retry_delays = (0.0, 0.0)
    backend.inbound_errors = [
        BridgeError("bridge_backend_transport_failed", retryable=True),
        BridgeError("bridge_backend_transport_failed", retryable=True),
        BridgeError("bridge_backend_transport_failed", retryable=True),
    ]

    await bridge.start()
    try:
        await bridge.accept_provider_event(_event("retry-exhausted", PHONE_A))
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
    finally:
        await bridge.stop()

    assert [name for name, _payload in backend.calls].count("inbound") == 3
    row = journal.connection.execute(
        "SELECT status, event_json FROM inbound_events WHERE provider_message_id = 'retry-exhausted'"
    ).fetchone()
    assert tuple(row) == ("quarantined", None)
    journal.close()


@async_test
async def test_worker_quarantines_nonretryable_inbound_failure_without_loop(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    backend.inbound_errors = [BridgeError("bridge_inbound_response_invalid")]

    await bridge.start()
    try:
        await bridge.accept_provider_event(_event("terminal-in-process", PHONE_A))
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
    finally:
        await bridge.stop()

    assert [name for name, _payload in backend.calls].count("inbound") == 1
    row = journal.connection.execute(
        "SELECT status, event_json FROM inbound_events WHERE provider_message_id = 'terminal-in-process'"
    ).fetchone()
    assert tuple(row) == ("quarantined", None)
    journal.close()


@async_test
async def test_bridge_start_keeps_registrar_up_while_provider_stream_is_starting(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    provider.is_healthy = False
    await bridge.start()
    assert bridge.provider_ready is False
    assert bridge.worker_task is None
    assert bridge.heartbeat_task is not None
    assert provider.handler is not None
    assert bridge.health_status == "starting"
    status_code, response = await bridge.registrar.register(str(uuid.uuid4()), PHONE_A)
    assert status_code == 201
    assert response["assigned_destination"] == LINE_A
    await bridge.stop()
    journal.close()


@async_test
async def test_delivery_persists_send_start_then_sends_once_and_acks(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    receipt_id, delivery_key = _delivery_ids()
    backend.inbound_result = {
        "status": "awaiting_delivery",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 3,
        "model_invoked": True,
    }
    backend.start_result = {
        "status": "sending",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 3,
        "text": "bounded reply",
    }
    await bridge.accept_provider_event(_event("one-send", PHONE_A))
    message_id = await bridge.queue.get()
    await bridge._process(message_id)
    assert backend.order == ["inbound", "delivery_start", "delivery_ack"]
    assert provider.order == ["provider_send"]
    assert provider.send_calls == [(PHONE_A, "bounded reply")]
    delivery = journal.connection.execute("SELECT * FROM deliveries").fetchone()
    assert delivery["provider_send_started"] == 1
    assert delivery["provider_message_id"] == "provider-outbound-1"
    assert delivery["status"] == "delivered"
    assert delivery["reply_text"] is None
    journal.close()


@async_test
async def test_missing_provider_message_id_is_uncertain_and_never_resent(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    receipt_id, delivery_key = _delivery_ids()
    backend.inbound_result = {
        "status": "awaiting_delivery",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 1,
        "model_invoked": True,
    }
    backend.start_result = {
        "status": "sending",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 1,
        "text": "reply",
    }
    provider.send_result = None
    await bridge.accept_provider_event(_event("null-provider-id", PHONE_A))
    await bridge._process(await bridge.queue.get())
    await bridge.reconcile()
    assert provider.send_calls == [(PHONE_A, "reply")]
    assert [name for name, _payload in backend.calls].count("delivery_uncertain") == 1
    delivery = journal.connection.execute("SELECT status, reply_text FROM deliveries").fetchone()
    assert tuple(delivery) == ("uncertain", None)
    journal.close()


@async_test
async def test_restart_marks_started_without_provider_id_uncertain_and_retries_ack_only(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    identity = bridge._transport_identity(journal.registration_for_handset(PHONE_A))
    first_receipt, first_key = _delivery_ids()
    first = journal.prepare_delivery(
        response={
            "receipt_id": first_receipt,
            "delivery_idempotency_key": first_key,
            "binding_generation": 1,
            "text": "maybe sent",
        },
        handset_e164=PHONE_A,
        **identity,
    )
    journal.mark_provider_send_started(first_receipt)
    second_receipt, second_key = _delivery_ids()
    second = journal.prepare_delivery(
        response={
            "receipt_id": second_receipt,
            "delivery_idempotency_key": second_key,
            "binding_generation": 1,
            "text": "already sent",
        },
        handset_e164=PHONE_A,
        **identity,
    )
    journal.mark_provider_send_started(second_receipt)
    journal.record_provider_message(second_receipt, "provider-id-persisted")
    await bridge.reconcile()
    assert provider.send_calls == []
    assert [name for name, _payload in backend.calls] == ["delivery_uncertain", "delivery_ack"]
    assert (
        journal.connection.execute(
            "SELECT status FROM deliveries WHERE receipt_id = ?", (first["receipt_id"],)
        ).fetchone()[0]
        == "uncertain"
    )
    assert (
        journal.connection.execute(
            "SELECT status FROM deliveries WHERE receipt_id = ?", (second["receipt_id"],)
        ).fetchone()[0]
        == "delivered"
    )
    journal.close()


@async_test
async def test_ack_transport_failure_retries_ack_only_without_second_provider_send(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    receipt_id, delivery_key = _delivery_ids()
    backend.inbound_result = {
        "status": "awaiting_delivery",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 4,
        "model_invoked": True,
    }
    backend.start_result = {
        "status": "sending",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 4,
        "text": "one provider send",
    }
    backend.delivery_ack_error = BridgeError("bridge_backend_transport_failed")
    await bridge.accept_provider_event(_event("ack-failure", PHONE_A))
    with pytest.raises(BridgeError, match="bridge_backend_transport_failed"):
        await bridge._process(await bridge.queue.get())
    assert provider.send_calls == [(PHONE_A, "one provider send")]
    assert journal.open_deliveries()[0]["status"] == "ack_pending"
    backend.delivery_ack_error = None
    await bridge.reconcile()
    assert provider.send_calls == [(PHONE_A, "one provider send")]
    assert journal.open_deliveries() == []
    journal.close()


@async_test
async def test_lost_delivery_start_response_is_quarantined_without_provider_send(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    receipt_id, delivery_key = _delivery_ids()
    backend.inbound_result = {
        "status": "awaiting_delivery",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 7,
        "model_invoked": True,
    }
    backend.delivery_start_error = BridgeError("bridge_backend_transport_failed")
    await bridge.accept_provider_event(_event("lost-start", PHONE_A))
    provider_message_id = await bridge.queue.get()
    with pytest.raises(BridgeError, match="bridge_backend_transport_failed"):
        await bridge._process(provider_message_id)
    backend.delivery_start_error = None
    backend.inbound_result = {
        "status": "sending",
        "receipt_id": receipt_id,
        "delivery_idempotency_key": delivery_key,
        "binding_generation": 7,
        "model_invoked": True,
    }
    await bridge._process(provider_message_id)
    assert provider.send_calls == []
    assert [name for name, _payload in backend.calls].count("delivery_uncertain") == 1
    journal.close()


@async_test
async def test_deregister_quarantines_backend_then_disables_local_mapping(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    await bridge.deregister_all()
    assert [name for name, _payload in backend.calls] == ["deregister"]
    assert journal.registration_for_handset(PHONE_A) is None
    journal.close()


@async_test
async def test_cold_stream_persists_first_event_then_promotes_before_model_work(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    provider.is_healthy = False
    await bridge.start()
    client = TestClient(TestServer(BridgeHttpServer(bridge).application()))
    await client.start_server()
    try:
        assert bridge.health_status == "starting"
        starting = await client.get("/healthz")
        assert starting.status == 503
        assert await starting.json() == {"status": "starting"}
        provider.is_healthy = True
        assert provider.handler is not None
        await provider.handler(_event("cold-first-event", PHONE_A))
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
        call_names = [name for name, _payload in backend.calls]
        assert call_names.count("inbound") == 1
        assert call_names.index("heartbeat") < call_names.index("inbound")
        assert bridge.health_status == "ready"
        ready = await client.get("/healthz")
        assert ready.status == 200
        assert await ready.json() == {"status": "ready"}
    finally:
        await client.close()
        await bridge.stop()
        journal.close()


@async_test
async def test_cold_stream_backend_failure_retains_event_for_later_reconcile(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    provider.is_healthy = False
    await bridge.start()
    try:
        provider.is_healthy = True
        backend.heartbeat_error = BridgeError("bridge_backend_transport_failed", retryable=True)
        assert provider.handler is not None
        await provider.handler(_event("cold-backend-down", PHONE_A))
        assert bridge.worker_task is None
        assert journal.pending_inbound()[0]["provider_message_id"] == "cold-backend-down"
        backend.heartbeat_error = None
        for _ in range(50):
            if bridge.worker_task is not None:
                break
            await asyncio.sleep(0.01)
        assert bridge.worker_task is not None
        await asyncio.wait_for(bridge.queue.join(), timeout=1)
        assert [name for name, _payload in backend.calls].count("inbound") == 1
    finally:
        await bridge.stop()
        journal.close()


@async_test
async def test_health_degrades_on_backend_failure_and_heartbeat_expiry(tmp_path: Path) -> None:
    now = [100.0]
    config = _config(tmp_path, heartbeat_freshness_seconds=0.02)
    journal = BridgeJournal(config.state_directory, config.project_id)
    provider = FakeProvider()
    backend = FakeBackend()
    bridge = ImessagePhotonBridge(
        config=config,
        journal=journal,
        provider=provider,
        backend=backend,
        connection_id="connection-health",
        monotonic=lambda: now[0],
    )
    _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    await bridge.start()
    client = TestClient(TestServer(BridgeHttpServer(bridge).application()))
    await client.start_server()
    try:
        assert (await client.get("/healthz")).status == 200
        backend.heartbeat_error = BridgeError("bridge_backend_transport_failed", retryable=True)
        await bridge._heartbeat_ready_registrations(ignore_errors=True)
        assert (await client.get("/healthz")).status == 503
        backend.heartbeat_error = None
        await bridge._heartbeat_ready_registrations(ignore_errors=False)
        assert (await client.get("/healthz")).status == 200
        now[0] += 0.03
        expired = await client.get("/healthz")
        assert expired.status == 503
        assert await expired.json() == {"status": "degraded"}
    finally:
        await client.close()
        await bridge.stop()
        journal.close()


@async_test
async def test_provider_fatal_marks_health_degraded_and_requests_process_restart(tmp_path: Path) -> None:
    bridge, journal, provider, _backend = _bridge(tmp_path)
    await bridge.start()
    try:
        assert bridge.health_status == "ready"
        assert provider.fatal_handler is not None
        await provider.fatal_handler()
        assert bridge.fatal_event.is_set()
        assert bridge.health_status == "degraded"
    finally:
        await bridge.stop()
        journal.close()


@async_test
async def test_provider_send_uses_one_plain_text_operation_for_url_only_reply() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def _sidecar_call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((path, body))
            return {"ok": True, "messageId": "provider-message-one"}

    adapter = Adapter()
    result = await send_provider_text_once(adapter, PHONE_A, "https://example.com")
    assert result == "provider-message-one"
    assert adapter.calls == [("/send", {"spaceId": PHONE_A, "text": "https://example.com", "format": "text"})]


@async_test
async def test_provider_send_lost_response_never_issues_fallback_operation() -> None:
    class Adapter:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def _sidecar_call(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
            self.calls.append((path, body))
            raise TimeoutError("synthetic lost response")

    adapter = Adapter()
    with pytest.raises(BridgeError, match="bridge_provider_send_uncertain"):
        await send_provider_text_once(adapter, PHONE_A, "https://example.com")
    assert len(adapter.calls) == 1
    assert adapter.calls[0][0] == "/send"


@async_test
async def test_prepared_restart_reuses_original_connection_fence_without_provider_resend(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    registration = _ready_registration(journal)
    receipt_id, delivery_key = _delivery_ids()
    identity = bridge._transport_identity(registration)
    intent = journal.prepare_delivery_intent(
        response={
            "receipt_id": receipt_id,
            "delivery_idempotency_key": delivery_key,
            "binding_generation": 9,
        },
        handset_e164=PHONE_A,
        **identity,
    )
    restarted = ImessagePhotonBridge(
        config=bridge.config,
        journal=journal,
        provider=provider,
        backend=backend,
        connection_id="connection-after-restart",
    )
    backend.delivery_start_error = BridgeError("imessage_delivery_outcome_uncertain", status_code=409)

    await restarted.reconcile()

    uncertain = next(payload for name, payload in backend.calls if name == "delivery_uncertain")
    assert uncertain["connection_id"] == intent["connection_id"] == "connection-1"
    assert provider.send_calls == []
    assert journal.delivery_by_receipt(receipt_id)["status"] == "uncertain"
    journal.close()


@async_test
async def test_scoped_cleanup_purges_only_selected_registration_and_is_retry_safe(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    first = _ready_registration(journal, phone=PHONE_A, line=LINE_A, provider_user_id="cleanup-a")
    second = _ready_registration(journal, phone=PHONE_B, line=LINE_B, provider_user_id="cleanup-b")
    journal.record_inbound(_normalize_provider_event(PROJECT_ID, _event("cleanup-in-a", PHONE_A)))
    journal.record_inbound(_normalize_provider_event(PROJECT_ID, _event("cleanup-in-b", PHONE_B)))
    receipt_a, key_a = _delivery_ids()
    receipt_b, key_b = _delivery_ids()
    journal.prepare_delivery(
        response={
            "receipt_id": receipt_a,
            "delivery_idempotency_key": key_a,
            "binding_generation": 1,
            "text": "cleanup a",
        },
        handset_e164=PHONE_A,
        **bridge._transport_identity(first),
    )
    journal.prepare_delivery(
        response={
            "receipt_id": receipt_b,
            "delivery_idempotency_key": key_b,
            "binding_generation": 1,
            "text": "cleanup b",
        },
        handset_e164=PHONE_B,
        **bridge._transport_identity(second),
    )

    receipt = await bridge.cleanup_registration(str(first["provider_request_id"]))
    duplicate = await bridge.cleanup_registration(str(first["provider_request_id"]))

    assert receipt["provider_disposition"] == "provider_user_retained_unbound"
    assert duplicate == receipt
    assert [name for name, _payload in backend.calls].count("deregister") == 1
    assert journal.registration_for_handset(PHONE_A) is None
    assert journal.registration_for_handset(PHONE_B) is not None
    assert (
        journal.connection.execute("SELECT COUNT(*) FROM inbound_events WHERE handset_e164 = ?", (PHONE_A,)).fetchone()[
            0
        ]
        == 0
    )
    assert (
        journal.connection.execute("SELECT COUNT(*) FROM inbound_events WHERE handset_e164 = ?", (PHONE_B,)).fetchone()[
            0
        ]
        == 1
    )
    assert journal.delivery_by_receipt(receipt_a) is None
    assert journal.delivery_by_receipt(receipt_b) is not None
    journal.close()


@async_test
async def test_cleanup_drains_inflight_work_and_blocks_late_provider_journal(tmp_path: Path) -> None:
    bridge, journal, provider, backend = _bridge(tmp_path)
    registration = _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    backend.release_inbound = asyncio.Event()
    await bridge.start()
    try:
        await bridge.accept_provider_event(_event("cleanup-inflight", PHONE_A))
        await asyncio.wait_for(backend.inbound_entered.wait(), timeout=1)

        cleanup = asyncio.create_task(bridge.cleanup_registration(str(registration["provider_request_id"])))
        for _ in range(50):
            if any(name == "deregister" for name, _payload in backend.calls):
                break
            await asyncio.sleep(0.01)
        assert any(name == "deregister" for name, _payload in backend.calls)
        assert not cleanup.done()

        assert provider.handler is not None
        late_event = asyncio.create_task(provider.handler(_event("cleanup-late", PHONE_A)))
        await asyncio.sleep(0)
        assert not late_event.done()

        backend.release_inbound.set()
        receipt = await asyncio.wait_for(cleanup, timeout=1)
        await asyncio.wait_for(late_event, timeout=1)
        await asyncio.wait_for(bridge.queue.join(), timeout=1)

        assert receipt["provider_disposition"] == "provider_user_retained_unbound"
        assert journal.registration_for_handset(PHONE_A) is None
        assert (
            journal.connection.execute(
                "SELECT COUNT(*) FROM inbound_events WHERE handset_e164 = ?", (PHONE_A,)
            ).fetchone()[0]
            == 0
        )
        assert journal.open_deliveries() == []
        assert [name for name, _payload in backend.calls].count("inbound") == 1
    finally:
        await bridge.stop()
        journal.close()


@async_test
async def test_reconcile_failure_never_publishes_ready_or_strands_a_live_worker(tmp_path: Path) -> None:
    bridge, journal, _provider, backend = _bridge(tmp_path)
    registration = _ready_registration(journal)
    journal.mark_proof_accepted(PHONE_A)
    receipt_id, delivery_key = _delivery_ids()
    journal.prepare_delivery_intent(
        response={
            "receipt_id": receipt_id,
            "delivery_idempotency_key": delivery_key,
            "binding_generation": 1,
        },
        handset_e164=PHONE_A,
        **bridge._transport_identity(registration),
    )
    backend.delivery_start_error = BridgeError("bridge_backend_transport_failed", retryable=True)

    with pytest.raises(BridgeError, match="bridge_backend_transport_failed"):
        await bridge.start()

    assert bridge.worker_task is None
    assert bridge.health_status == "degraded"
    assert journal.delivery_by_receipt(receipt_id)["status"] == "prepared"
    journal.close()


@async_test
async def test_cleanup_http_is_authenticated_content_free_and_reports_provider_retained(tmp_path: Path) -> None:
    bridge, journal, _provider, _backend = _bridge(tmp_path)
    registration = _ready_registration(journal)
    client = TestClient(TestServer(BridgeHttpServer(bridge).application()))
    await client.start_server()
    path = f"/v1/registrations/{registration['provider_request_id']}"
    try:
        denied = await client.delete(path)
        assert denied.status == 401
        accepted = await client.delete(path, headers={"Authorization": f"Bearer {TOKEN_B}"})
        assert accepted.status == 202
        assert accepted.headers["Cache-Control"] == "no-store"
        body = await accepted.json()
        assert body == {
            "status": "local_cleanup_complete_provider_retained",
            "provider_disposition": "provider_user_retained_unbound",
            "operator_action_required": True,
            "absence_proof": {"registrations": 0, "inbound_events": 0, "deliveries": 0},
        }
        assert PHONE_A not in str(body)
    finally:
        await client.close()
        journal.close()
