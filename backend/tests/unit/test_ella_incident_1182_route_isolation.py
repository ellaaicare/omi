import ast
import asyncio
import inspect
import sys
import textwrap
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

for module_name in (
    "database._client",
    "database.conversations",
    "database.memories",
    "database.users",
    "database.ella_contacts",
    "utils.notifications",
    "utils.other.storage",
):
    sys.modules.setdefault(module_name, MagicMock(db=MagicMock()))
sys.modules.setdefault("websockets", MagicMock())

import ella
from ella.routers import canonical_events, chat, resolve, trace
from scripts import ella_memory_e2e_smoke
from utils.ella import exact_firebase_auth

_BACKEND = Path(__file__).resolve().parents[2]


class RecordingCanonicalStore(canonical_events.CanonicalEventStore):
    def __init__(self):
        self.writes = []
        self.completions = []
        self.timeline_calls = []

    async def write_batch(self, events):
        self.writes.append(events)
        return {"ok": True, "inserted": len(events), "duplicates": 0, "events": []}

    async def complete_session(self, session_id, completion):
        self.completions.append((session_id, completion))
        return {"ok": True, "session_id": session_id}

    async def timeline(self, *, uid, since, limit, channels):
        self.timeline_calls.append((uid, since, limit, channels))
        return []


def _verify_firebase(token):
    if token == "token-a":
        return {"uid": "uid-a"}
    if token == "token-b":
        return {"uid": "uid-b"}
    raise ValueError("invalid token")


def _event(uid="uid-a", *, event_id="event-a"):
    return {
        "uid": uid,
        "canonical_identity": uid,
        "event_id": event_id,
        "session_id": "session-a",
        "channel": "ios_chat",
        "provider": "omi-backend",
        "role": "user",
        "text": "content-free test",
        "started_at": datetime.now(timezone.utc).isoformat(),
    }


def test_canonical_routes_reject_unauthenticated_and_cross_owner_before_store(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _verify_firebase)
    monkeypatch.setenv("ELLA_EVENT_LEDGER_TOKEN", "ledger-service-test")
    store = RecordingCanonicalStore()
    app = FastAPI()
    app.include_router(canonical_events.create_canonical_events_router(store))
    client = TestClient(app)

    assert client.post("/v1/ella/events", json={"events": []}).status_code == 401
    cross_batch = client.post(
        "/v1/ella/events",
        headers={"Authorization": "Bearer token-a"},
        json={"events": [_event(), _event("uid-b", event_id="event-b")]},
    )
    assert cross_batch.status_code == 403
    assert store.writes == []

    assert client.get("/v1/ella/timeline?uid=uid-a").status_code == 401
    assert (
        client.get(
            "/v1/ella/timeline?uid=uid-b",
            headers={"Authorization": "Bearer token-a"},
        ).status_code
        == 403
    )
    assert store.timeline_calls == []

    completion = {
        "uid": "uid-b",
        "canonical_identity": "uid-b",
        "channel": "ios_voice",
        "provider": "voice-proxy",
    }
    assert (
        client.post(
            "/v1/ella/sessions/session-a/complete",
            headers={"Authorization": "Bearer token-a"},
            json=completion,
        ).status_code
        == 403
    )
    assert store.completions == []

    owner_event = client.post(
        "/v1/ella/events",
        headers={"Authorization": "Bearer token-a"},
        json={"events": [_event()]},
    )
    assert owner_event.status_code == 200
    assert len(store.writes) == 1

    owner_timeline = client.get(
        "/v1/ella/timeline?uid=uid-a",
        headers={"Authorization": "Bearer token-a"},
    )
    assert owner_timeline.status_code == 200
    assert store.timeline_calls == [("uid-a", None, canonical_events.DEFAULT_TIMELINE_LIMIT, None)]

    owner_completion = client.post(
        "/v1/ella/sessions/session-a/complete",
        headers={"Authorization": "Bearer token-a"},
        json={**completion, "uid": "uid-a", "canonical_identity": "uid-a"},
    )
    assert owner_completion.status_code == 200
    assert len(store.completions) == 1


def test_canonical_service_validates_every_identity_and_preserves_internal_sync(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _verify_firebase)
    monkeypatch.setenv("ELLA_EVENT_LEDGER_TOKEN", "ledger-service-test")
    store = RecordingCanonicalStore()
    app = FastAPI()
    app.include_router(canonical_events.create_canonical_events_router(store))
    client = TestClient(app)
    headers = {
        "X-Ella-Event-Ledger-Key": "ledger-service-test",
        "X-Ella-Subject-Uid": "uid-a",
    }

    accepted = client.post(
        "/v1/ella/events",
        headers=headers,
        json={"events": [_event()]},
    )
    assert accepted.status_code == 200
    assert len(store.writes) == 1

    mixed_owner = client.post(
        "/v1/ella/events",
        headers=headers,
        json={"events": [_event(), _event("uid-b", event_id="event-b")]},
    )
    assert mixed_owner.status_code == 403
    assert len(store.writes) == 1

    malformed = _event(event_id="event-c")
    malformed["canonical_identity"] = "uid-b"
    rejected = client.post("/v1/ella/events", headers=headers, json={"events": [malformed]})
    assert rejected.status_code == 403
    assert len(store.writes) == 1

    uid_b_headers = {**headers, "X-Ella-Subject-Uid": "uid-b"}
    timeline = client.get("/v1/ella/timeline?uid=uid-b", headers=uid_b_headers)
    assert timeline.status_code == 200
    assert store.timeline_calls[0][0] == "uid-b"

    completion = client.post(
        "/v1/ella/sessions/session-b/complete",
        headers=uid_b_headers,
        json={
            "uid": "uid-b",
            "canonical_identity": "uid-b",
            "channel": "ios_voice",
            "provider": "voice-proxy",
        },
    )
    assert completion.status_code == 200
    assert store.completions[0][0] == "session-b"


def test_trace_routes_default_off_and_never_return_runtime_coordinates(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _verify_firebase)
    app = FastAPI()
    app.include_router(trace.router)
    client = TestClient(app)

    monkeypatch.delenv("ELLA_DEBUG_ROUTES_ENABLED", raising=False)
    assert client.get("/v1/ella/debug/traces").status_code == 401
    assert (
        client.get(
            "/v1/ella/debug/traces",
            headers={"Authorization": "Bearer token-a"},
        ).status_code
        == 404
    )

    monkeypatch.setenv("ELLA_DEBUG_ROUTES_ENABLED", "true")
    cross = client.get(
        "/v1/ella/debug/traces?uid=uid-b",
        headers={"Authorization": "Bearer token-a"},
    )
    assert cross.status_code == 403
    own = client.get(
        "/v1/ella/debug/traces?uid=uid-a",
        headers={"Authorization": "Bearer token-a"},
    )
    assert own.status_code == 200
    assert own.json() == {"source": "disabled", "count": 0, "total": 0, "traces": []}


def test_client_trace_uses_authenticated_uid_and_discards_caller_runtime_material(monkeypatch):
    monkeypatch.setattr(exact_firebase_auth.firebase_auth, "verify_id_token", _verify_firebase)
    recorded = []
    monkeypatch.setattr(trace, "record_trace", recorded.append)
    app = FastAPI()
    app.include_router(trace.router)
    client = TestClient(app)
    payload = {
        "uid": "uid-a",
        "clientType": "ios",
        "clientVersion": "1.0",
        "latencyMs": 10,
        "status": 200,
        "resolvedGateway": "http://caller-selected.invalid",
        "sessionKey": "caller-selected-session",
        "headers": {"authorization": "caller-selected-token"},
        "notes": ["caller-selected-content"],
    }

    assert client.post("/v1/ella/debug/client-trace", json=payload).status_code == 401
    cross = dict(payload, uid="uid-b")
    assert (
        client.post(
            "/v1/ella/debug/client-trace",
            headers={"Authorization": "Bearer token-a"},
            json=cross,
        ).status_code
        == 403
    )
    assert recorded == []

    accepted = client.post(
        "/v1/ella/debug/client-trace",
        headers={"Authorization": "Bearer token-a"},
        json=payload,
    )
    assert accepted.status_code == 200
    assert accepted.json()["retained"] is False
    assert len(recorded) == 1
    assert set(recorded[0].to_dict()) == {
        "traceId",
        "endpointClass",
        "method",
        "debugLevel",
        "responseStatus",
        "totalLatencyMs",
        "hasError",
    }
    serialized = str(recorded[0].to_dict())
    for sensitive in (
        "uid-a",
        "caller-selected",
        "resolvedGateway",
        "sessionKey",
        "headers",
        "notes",
    ):
        assert sensitive not in serialized


def test_trace_recording_is_synchronous_content_free_and_never_persists(monkeypatch):
    messages = []
    monkeypatch.setattr(trace.logger, "info", lambda *args: messages.append(args))

    class ForbiddenLoop:
        def create_task(self, *_args, **_kwargs):
            raise AssertionError("trace recording must not enqueue detached work")

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: ForbiddenLoop())
    event = trace.RouteTrace()
    event.endpoint_class = "chat"
    event.method = "POST"
    event.response_status = 200
    event.total_latency_ms = 12
    trace.record_trace(event)

    assert len(messages) == 1
    serialized = str(messages)
    for sensitive in (
        "private-gateway",
        "private-session",
        "private-token",
        "X-Ella-Route",
        "medical-note",
    ):
        assert sensitive not in serialized
    assert not hasattr(trace, "_persist_trace")
    assert "INSERT INTO routing_traces" not in (_BACKEND / "ella" / "routers" / "trace.py").read_text(encoding="utf-8")


def test_authenticated_chat_and_legacy_level4_cannot_record_caller_or_runtime_material(monkeypatch):
    recorded = []

    async def isolated_runtime(*_args, **_kwargs):
        return SimpleNamespace(provider="hermes", agent_id="private-agent")

    async def stream(*_args, **_kwargs):
        yield "done: content-free\n\n"

    monkeypatch.setattr(chat, "resolve_isolated_runtime", isolated_runtime)
    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(chat, "record_trace", recorded.append)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/ella/chat/stream",
            "headers": [(b"x-ella-private", b"private-token")],
            "client": ("127.0.0.1", 1234),
        }
    )
    response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(uid="uid-a", message="medical-note"),
            request,
            authenticated_uid="uid-a",
            x_ella_client_type="ios",
            x_ella_client_version="private-version",
            x_ella_route="private-route",
        )
    )

    assert response.status_code == 200
    assert len(recorded) == 1
    serialized = str(recorded[0].to_dict())
    for sensitive in (
        "uid-a",
        "medical-note",
        "private-token",
        "private-version",
        "private-route",
        "private-agent",
    ):
        assert sensitive not in serialized
    level4_source = inspect.getsource(chat._stream_level_4_openclaw)
    for forbidden_field in (
        "client_headers",
        "client_route",
        "resolved_gateway",
        "resolved_session_key",
        "resolved_agent",
        ".notes",
    ):
        assert forbidden_field not in level4_source


def test_post_auth_chat_output_never_contains_caller_selected_header_values(monkeypatch, capsys, caplog):
    private_markers = (
        "HOSTILE_PRIVATE_CLIENT_TYPE_1182",
        "HOSTILE_PRIVATE_CLIENT_VERSION_1182",
        "HOSTILE_PRIVATE_ROUTE_1182",
        "HOSTILE_PRIVATE_DEBUG_1182",
    )
    blocked_head_output = f"[FLOW:CHAT] client={private_markers[0]} request_received=true"
    assert private_markers[0] in blocked_head_output

    async def isolated_runtime(*_args, **_kwargs):
        return SimpleNamespace(provider="hermes", agent_id="bounded-agent")

    async def stream(*_args, **_kwargs):
        yield "done: content-free\n\n"

    monkeypatch.setattr(chat, "resolve_isolated_runtime", isolated_runtime)
    monkeypatch.setattr(chat, "_stream_hermes_chat", stream)
    monkeypatch.setattr(chat, "record_trace", lambda *_args, **_kwargs: None)
    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/ella/chat/stream",
            "headers": [],
            "client": ("127.0.0.1", 1234),
        }
    )
    response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(uid="uid-a", message="content-free test"),
            request,
            authenticated_uid="uid-a",
            x_ella_debug_level=private_markers[3],
            x_ella_client_type=private_markers[0],
            x_ella_client_version=private_markers[1],
            x_ella_route=private_markers[2],
        )
    )

    captured = capsys.readouterr()
    process_output = f"{captured.out}\n{captured.err}\n{caplog.text}"
    assert response.status_code == 200
    assert "[FLOW:CHAT]" in process_output
    assert chat._bounded_client_type(private_markers[0]) == "other"
    assert chat._bounded_client_type("iOS") == "ios"
    for marker in private_markers:
        assert marker not in process_output


def test_chat_caller_metadata_is_fixed_before_canonical_and_cloud_sinks(monkeypatch, capsys, caplog):
    markers = (
        "HOSTILE_TYPE_1182\r\n",
        "Hostile_Version_1182\x00",
        "HOSTILE_ROUTE_1182" * 128,
        "HOSTILE_DEBUG_1182\t",
    )
    canonical_events = []

    async def no_canonical_context(*_args, **_kwargs):
        return []

    async def no_temporal_context(*_args, **_kwargs):
        return None, []

    async def runtime_authority_disabled(*_args, **_kwargs):
        return False

    async def capture_canonical_event(event):
        canonical_events.append(event)

    class StreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def aiter_lines(self):
            yield 'data: {"choices":[{"delta":{"content":"ok"}}]}'
            yield 'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}'
            yield "data: [DONE]"

    class StreamClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def stream(self, *_args, **_kwargs):
            return StreamResponse()

    monkeypatch.setattr(chat, "resolve_isolated_runtime", lambda *_args, **_kwargs: _async_value(None))
    monkeypatch.setattr(chat, "_retained_owner_chat_configured", lambda _uid: True)
    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", no_canonical_context)
    monkeypatch.setattr(chat, "_fetch_temporal_chat_context", no_temporal_context)
    monkeypatch.setattr(chat, "runtime_authority_enabled", runtime_authority_disabled)
    monkeypatch.setattr(chat, "_write_ios_chat_canonical_event", capture_canonical_event)
    monkeypatch.setattr(chat.httpx, "AsyncClient", StreamClient)
    monkeypatch.setattr(chat, "HERMES_GATEWAY_TOKEN", "configured-test-gateway-token")
    monkeypatch.setattr(chat, "HERMES_GATEWAY_URL", "http://runtime.test")
    monkeypatch.setattr(chat, "HERMES_MODEL", "hermes")
    monkeypatch.setattr(chat, "record_trace", lambda *_args, **_kwargs: None)
    request = Request({"type": "http", "method": "POST", "path": "/v1/ella/chat/stream", "headers": []})
    response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(uid="uid-a", message="content-free"),
            request,
            authenticated_uid="uid-a",
            x_ella_debug_level=markers[3],
            x_ella_client_type=markers[0],
            x_ella_client_version=markers[1],
            x_ella_route=markers[2],
        )
    )
    asyncio.run(_collect_response_body(response))

    assert len(canonical_events) == 2
    assert {event.role for event in canonical_events} == {"user", "assistant"}
    assert all(
        event.metadata["client"] == {"type": "other", "route": chat.CHAT_SERVER_ROUTE_CATEGORY}
        for event in canonical_events
    )

    cloud_requests = []
    cloud_runtime = SimpleNamespace(provider="hermes_cloud", binding_id="binding-a")

    async def cloud_runtime_for_user(*_args, **_kwargs):
        return cloud_runtime

    async def create_repository():
        return SimpleNamespace()

    class CloudRuntimeService:
        def __init__(self, **_kwargs):
            pass

        async def run_turn(self, _runtime, turn_request):
            cloud_requests.append(turn_request)
            return SimpleNamespace(
                text="ok",
                canonical_assistant_event_id="assistant-event-a",
                duplicate=False,
                response_id="response-a",
            )

    monkeypatch.setattr(chat, "resolve_isolated_runtime", cloud_runtime_for_user)
    monkeypatch.setattr(chat.EllaProvisioningRepository, "create", create_repository)
    monkeypatch.setattr(chat, "HermesCloudRuntimeService", CloudRuntimeService)
    cloud_response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(uid="uid-a", message="content-free"),
            request,
            authenticated_uid="uid-a",
            x_ella_debug_level=markers[3],
            x_ella_client_type=" \tIoS\r\n",
            x_ella_client_version=markers[1],
            x_ella_route=markers[2],
        )
    )
    asyncio.run(_collect_response_body(cloud_response))

    assert len(cloud_requests) == 1
    assert cloud_requests[0].client_metadata == {
        "type": "ios",
        "route": chat.CHAT_SERVER_ROUTE_CATEGORY,
    }
    assert chat._server_owned_client_metadata("ANDROID")["type"] == "android"
    assert chat._server_owned_client_metadata(" web ")["type"] == "web"

    captured = capsys.readouterr()
    all_sinks = f"{canonical_events!r}\n{cloud_requests!r}\n{captured.out}\n{captured.err}\n{caplog.text}"
    for marker in markers:
        assert marker not in all_sinks


async def _async_value(value):
    return value


def test_unbound_non_owner_chat_fails_before_trace_or_provider(monkeypatch):
    effects = []

    async def no_runtime(*_args, **_kwargs):
        effects.append("resolver")
        return None

    monkeypatch.setattr(chat, "resolve_isolated_runtime", no_runtime)
    monkeypatch.setattr(chat, "retained_owner_uid_configured", lambda _uid: False)
    monkeypatch.setattr(chat, "record_trace", lambda *_args, **_kwargs: effects.append("trace"))

    class ForbiddenClient:
        def __init__(self, *_args, **_kwargs):
            effects.append("provider")
            raise AssertionError("unbound user reached provider")

    monkeypatch.setattr(chat.httpx, "AsyncClient", ForbiddenClient)
    request = Request({"type": "http", "method": "POST", "path": "/v1/ella/chat/stream", "headers": []})
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            chat.ella_chat_stream(
                chat.EllaChatRequest(uid="uid-a", message="content-free test"),
                request,
                authenticated_uid="uid-a",
            )
        )
    assert error.value.status_code == 409
    assert error.value.detail == {"code": "hermes_runtime_required"}
    assert effects == ["resolver"]


def test_retained_owner_chat_requires_exact_subject_and_complete_server_configuration(monkeypatch):
    effects = []

    async def no_runtime(*_args, **_kwargs):
        effects.append("resolver")
        return None

    async def retained_stream(*_args, **_kwargs):
        effects.append("retained-stream")
        yield "done: retained-owner\n\n"

    monkeypatch.setattr(chat, "resolve_isolated_runtime", no_runtime)
    monkeypatch.setattr(chat, "retained_owner_uid_configured", lambda uid: uid == "owner-uid")
    monkeypatch.setattr(chat, "HERMES_GATEWAY_URL", "http://retained.internal:8642")
    monkeypatch.setattr(chat, "HERMES_GATEWAY_TOKEN", "configured-retained-token")
    monkeypatch.setattr(chat, "HERMES_MODEL", "plato-eval")
    monkeypatch.setattr(chat, "CHAT_PLATFORM", "hermes")
    monkeypatch.setattr(chat, "_stream_hermes_chat", retained_stream)
    monkeypatch.setattr(chat, "record_trace", lambda *_args, **_kwargs: None)
    request = Request({"type": "http", "method": "POST", "path": "/v1/ella/chat/stream", "headers": []})

    response = asyncio.run(
        chat.ella_chat_stream(
            chat.EllaChatRequest(uid="owner-uid", message="content-free test"),
            request,
            authenticated_uid="owner-uid",
        )
    )
    assert response.status_code == 200
    assert asyncio.run(_collect_response_body(response)) == ["done: retained-owner\n\n"]
    assert effects == ["resolver", "retained-stream"]

    monkeypatch.setattr(chat, "HERMES_MODEL", "")
    with pytest.raises(HTTPException) as error:
        asyncio.run(
            chat.ella_chat_stream(
                chat.EllaChatRequest(uid="owner-uid", message="content-free test"),
                request,
                authenticated_uid="owner-uid",
            )
        )
    assert error.value.detail == {"code": "hermes_runtime_required"}


async def _collect_response_body(response):
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
    return chunks


def test_legacy_routing_is_never_returned_for_non_owner_without_binding(monkeypatch):
    class Pool:
        async def fetchrow(self, *_args):
            return {
                "id": "db-user-a",
                "name": "A",
                "omi_uid": "uid-a",
                "status": "ACTIVE",
                "guardian_mode": "off",
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
                "agents": {
                    "userAgentId": "owner-agent",
                    "gatewayToken": "owner-token",
                    "workspace": "/owner/workspace",
                },
                "cluster_status": "active",
            }

    async def no_runtime(*_args, **_kwargs):
        return None

    async def pool():
        return Pool()

    monkeypatch.setattr(resolve, "_get_pool", pool)
    monkeypatch.setattr(resolve, "resolve_isolated_runtime", no_runtime)
    monkeypatch.setattr(resolve, "retained_owner_uid_configured", lambda _uid: False)
    result = asyncio.run(resolve.resolve_user_routing("uid-a"))
    assert result["routing"] is None
    serialized = str(result)
    assert "owner-agent" not in serialized
    assert "owner-token" not in serialized
    assert "/owner/workspace" not in serialized


def _run_real_smoke_seed(monkeypatch, *, canonical_identity="uid-a"):
    monkeypatch.setenv("ELLA_EVENT_LEDGER_TOKEN", "ledger-service-test")
    store = RecordingCanonicalStore()
    app = FastAPI()
    app.include_router(canonical_events.create_canonical_events_router(store))
    client = TestClient(app)
    args = ella_memory_e2e_smoke.parse_args(
        [
            "--uid",
            "uid-a",
            "--canonical-identity",
            canonical_identity,
            "--mcp-canonical-identity",
            "plato",
            "--seed-multichannel",
        ]
    )
    smoke = ella_memory_e2e_smoke.MemorySmoke(args)

    def mounted_request(method, url, payload=None, headers=None):
        response = client.request(method, url.removeprefix(smoke.backend_url), json=payload, headers=headers)
        return response.json(), 1

    monkeypatch.setattr(ella_memory_e2e_smoke, "_json_request", mounted_request)
    monkeypatch.setattr(
        smoke,
        "search_memory",
        lambda *_args, **_kwargs: [
            {"channel": event.channel, "text": event.text} for batch in store.writes for event in batch
        ],
    )
    return smoke, store


def test_real_smoke_multichannel_payload_uses_exact_uid_with_ledger_authority(monkeypatch):
    defaults = ella_memory_e2e_smoke.parse_args(["--uid", "uid-a", "--seed-multichannel"])
    assert defaults.canonical_identity == defaults.uid
    assert defaults.mcp_canonical_identity == "plato"

    smoke, store = _run_real_smoke_seed(monkeypatch)
    smoke.seed_multichannel_events()
    assert len(store.writes) == 1
    assert len(store.writes[0]) == 3
    assert {event.uid for event in store.writes[0]} == {"uid-a"}
    assert {event.canonical_identity for event in store.writes[0]} == {"uid-a"}


def test_real_smoke_multichannel_identity_mismatch_has_zero_writes(monkeypatch):
    smoke, store = _run_real_smoke_seed(monkeypatch, canonical_identity="uid-b")
    with pytest.raises(ella_memory_e2e_smoke.SmokeFailure, match="multi-channel event write failed"):
        smoke.seed_multichannel_events()
    assert store.writes == []


def _authority_id(callable_object):
    return f"{callable_object.__module__}:{callable_object.__qualname__}"


def _contract(endpoint, *dependencies, manual=None):
    return endpoint, tuple(dependencies), manual


MOUNTED_ROUTE_CONTRACT = {
    ("callbacks", "PATCH", "/v1/ella/conversation/{conversation_id}/summary"): _contract(
        "update_conversation_summary", "ella.routers.callbacks:require_callback_service"
    ),
    ("callbacks", "GET", "/v1/ella/conversations/enrichment/reconcile-candidates"): _contract(
        "list_enrichment_reconcile_candidates", "ella.routers.callbacks:require_callback_service"
    ),
    ("callbacks", "GET", "/v1/ella/conversation/{conversation_id}/data"): _contract(
        "get_conversation_data", "ella.routers.callbacks:require_callback_service"
    ),
    ("callbacks", "GET", "/v1/ella/conversation/summary/capabilities"): _contract(
        "conversation_summary_capabilities", manual="public_static_capability"
    ),
    ("callbacks", "GET", "/v1/ella/health"): _contract("ella_health", manual="public_minimal_health"),
    ("callbacks", "POST", "/v1/ella/notification"): _contract(
        "ella_notification", "ella.routers.callbacks:require_callback_service"
    ),
    ("callbacks", "POST", "/v1/ella/emergency"): _contract(
        "ella_emergency", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "POST", "/v1/ella/daily-summary"): _contract(
        "ella_daily_summary", "ella.routers.callbacks:require_callback_service"
    ),
    ("callbacks", "POST", "/v1/ella/emergency-contact"): _contract(
        "create_emergency_contact", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "GET", "/v1/ella/emergency-contacts/{uid}"): _contract(
        "list_emergency_contacts", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "PUT", "/v1/ella/emergency-contact/{contact_id}"): _contract(
        "update_emergency_contact", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "DELETE", "/v1/ella/emergency-contact/{contact_id}"): _contract(
        "delete_emergency_contact", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "GET", "/v1/ella/caregivers"): _contract(
        "list_caregivers", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "POST", "/v1/ella/caregivers/invite"): _contract(
        "invite_caregiver", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "GET", "/v1/ella/caregivers/emergency-contact"): _contract(
        "get_emergency_caregiver", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "PUT", "/v1/ella/caregivers/emergency-contact"): _contract(
        "update_emergency_caregiver", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "PUT", "/v1/ella/caregivers/{caregiver_id}/permissions"): _contract(
        "update_caregiver_permissions", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "POST", "/v1/ella/caregivers/{caregiver_id}/resend-invite"): _contract(
        "resend_caregiver_invite", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "DELETE", "/v1/ella/caregivers/{caregiver_id}"): _contract(
        "remove_caregiver", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("callbacks", "GET", "/v1/ella/caregiver-dashboard-data"): _contract(
        "caregiver_dashboard_data", manual="validate_dashboard_token"
    ),
    ("callbacks", "POST", "/v1/ella/generate-dashboard-token"): _contract(
        "generate_dashboard_token_endpoint", "ella.routers.callbacks:require_caregiver_service"
    ),
    ("chat", "POST", "/v1/ella/chat/stream"): _contract(
        "ella_chat_stream", "ella.services.ai_consent:require_current_ai_consent"
    ),
    ("chat", "GET", "/v1/ella/chat/history"): _contract(
        "ella_chat_history", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("resolve", "GET", "/v1/ella/resolve"): _contract(
        "resolve_endpoint", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("resolve", "GET", "/v1/ella/chat/history/{agent_id}"): _contract(
        "proxy_chat_history", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("trace", "POST", "/v1/ella/debug/client-trace"): _contract(
        "ingest_client_trace", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("trace", "GET", "/v1/ella/debug/traces"): _contract(
        "get_traces",
        "utils.ella.exact_firebase_auth:get_exact_firebase_uid",
        "ella.routers.trace:_require_debug_reads_enabled",
    ),
    ("trace", "GET", "/v1/ella/debug/trace/{uid}"): _contract(
        "get_user_traces",
        "utils.ella.exact_firebase_auth:get_exact_firebase_uid",
        "ella.routers.trace:_require_debug_reads_enabled",
    ),
    ("trace", "GET", "/v1/ella/debug/stats"): _contract(
        "trace_stats",
        "utils.ella.exact_firebase_auth:get_exact_firebase_uid",
        "ella.routers.trace:_require_debug_reads_enabled",
    ),
    ("trace", "GET", "/v1/ella/debug/status"): _contract(
        "debug_status",
        "utils.ella.exact_firebase_auth:get_exact_firebase_uid",
        "ella.routers.trace:_require_debug_reads_enabled",
    ),
    ("trace", "GET", "/v1/ella/debug/console"): _contract(
        "debug_console",
        "utils.ella.exact_firebase_auth:get_exact_firebase_uid",
        "ella.routers.trace:_require_debug_reads_enabled",
    ),
    ("debug_metadata", "GET", "/v1/ella/debug/conversations/metadata"): _contract(
        "list_conversation_metadata", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("debug_metadata", "GET", "/v1/ella/debug/conversations/{conversation_id}/metadata"): _contract(
        "read_conversation_metadata", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("canonical_events", "POST", "/v1/ella/events"): _contract(
        "write_events", "ella.routers.canonical_events:_canonical_event_authority"
    ),
    ("canonical_events", "POST", "/v1/ella/sessions/{session_id}/complete"): _contract(
        "complete_session",
        "ella.routers.canonical_events:_canonical_event_authority",
    ),
    ("canonical_events", "GET", "/v1/ella/timeline"): _contract(
        "read_timeline", "ella.routers.canonical_events:_canonical_event_authority"
    ),
    ("voice", "GET", "/v1/voice/providers"): _contract(
        "get_voice_providers", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("voice", "POST", "/v1/voice/session"): _contract(
        "create_voice_session", "ella.services.ai_consent:require_current_ai_consent"
    ),
    ("voice", "GET", "/v1/voice/entitlement"): _contract(
        "get_voice_entitlement", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("voice", "POST", "/v1/voice/canary/accept"): _contract(
        "accept_voice_canary_session", manual="authenticate_voice_proxy_request"
    ),
    ("voice", "POST", "/v1/voice/canary/heartbeat"): _contract(
        "heartbeat_voice_canary_session", manual="authenticate_voice_proxy_request"
    ),
    ("voice", "POST", "/v1/voice/canary/complete"): _contract(
        "complete_voice_canary_session", manual="authenticate_voice_proxy_request"
    ),
    ("voice", "GET", "/v1/voice/config"): _contract("get_voice_config", manual="public_static_config"),
    ("voice", "POST", "/v1/voice/tts"): _contract(
        "synthesize_speech", "ella.services.ai_consent:require_current_ai_consent_or_internal_tts"
    ),
    ("voice", "GET", "/v1/voice/health"): _contract(
        "voice_health", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
    ("voice", "POST", "/v1/voice/context"): _contract("get_voice_context", manual="authenticate_voice_proxy_request"),
    ("voice", "POST", "/v1/voice/tool"): _contract("execute_voice_tool", manual="authenticate_voice_proxy_request"),
    ("voice", "POST", "/v1/voice/search-omi"): _contract(
        "search_omi_conversations", manual="authenticate_voice_proxy_request"
    ),
    ("voice", "POST", "/v1/voice/search"): _contract("unified_search", manual="authenticate_voice_proxy_request"),
    ("voice", "GET", "/v1/entitlement"): _contract(
        "get_voice_entitlement", "utils.ella.exact_firebase_auth:get_exact_firebase_uid"
    ),
}

MANUAL_AUTHORITY_CONTRACT = {
    "validate_dashboard_token": "ella.routers.callbacks:validate_dashboard_token",
    "authenticate_voice_proxy_request": "ella.routers.voice:authenticate_voice_proxy_request",
}


def _direct_call_names(endpoint):
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    return {node.func.id for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}


def _mounted_affected_routes(monkeypatch):
    monkeypatch.setattr(ella, "ELLA_VOICE_V2_ENABLED", True)
    monkeypatch.setattr(ella, "ELLA_GUARDIAN_ENABLED", False)
    monkeypatch.setattr(ella, "ELLA_TESTING_ENABLED", False)
    app = FastAPI()
    ella._register_routers(app)
    affected_modules = {f"ella.routers.{name}" for name in {key[0] for key in MOUNTED_ROUTE_CONTRACT}}
    return [
        route for route in app.routes if isinstance(route, APIRoute) and route.endpoint.__module__ in affected_modules
    ]


def test_real_mounted_route_manifest_has_exact_paths_authorities_and_no_duplicates(monkeypatch):
    routes = _mounted_affected_routes(monkeypatch)
    actual = []
    path_methods = []
    for route in routes:
        module = route.endpoint.__module__.removeprefix("ella.routers.")
        for method in sorted(route.methods or ()):
            key = (module, method, route.path)
            actual.append(key)
            path_methods.append((method, route.path))
            assert key in MOUNTED_ROUTE_CONTRACT, key
            endpoint_name, dependencies, manual = MOUNTED_ROUTE_CONTRACT[key]
            assert route.endpoint.__name__ == endpoint_name
            assert tuple(_authority_id(item.call) for item in route.dependant.dependencies) == dependencies
            if manual in {"public_minimal_health", "public_static_capability", "public_static_config"}:
                assert dependencies == ()
            elif manual:
                assert manual in _direct_call_names(route.endpoint)
                assert _authority_id(route.endpoint.__globals__[manual]) == MANUAL_AUTHORITY_CONTRACT[manual]
            else:
                assert dependencies, f"unclassified authority: {key}"

    assert set(actual) == set(MOUNTED_ROUTE_CONTRACT)
    assert len(actual) == len(MOUNTED_ROUTE_CONTRACT) == 50
    assert len(path_methods) == len(set(path_methods)), Counter(path_methods)


def _background_boundaries():
    boundaries = Counter()
    for module in {key[0] for key in MOUNTED_ROUTE_CONTRACT}:
        tree = ast.parse((_BACKEND / "ella" / "routers" / f"{module}.py").read_text(encoding="utf-8"))
        parents = {}
        for node in ast.walk(tree):
            for child in ast.iter_child_nodes(node):
                parents[child] = node
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            kind = None
            if isinstance(node.func, ast.Attribute) and node.func.attr in {
                "create_task",
                "to_thread",
                "add_task",
                "Thread",
            }:
                prefix = node.func.value.id if isinstance(node.func.value, ast.Name) else ""
                kind = f"{prefix}.{node.func.attr}".lstrip(".")
            elif isinstance(node.func, ast.Name) and node.func.id in {"create_task", "to_thread", "Thread"}:
                kind = node.func.id
            if kind is None:
                continue
            parent = node
            function_name = "<module>"
            while parent in parents:
                parent = parents[parent]
                if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    function_name = parent.name
                    break
            boundaries[(module, function_name, kind)] += 1
    return boundaries


def test_affected_background_boundaries_are_exact_and_trace_has_no_detached_task():
    assert _background_boundaries() == Counter(
        {
            ("chat", "_stream_level_4_openclaw", "asyncio.create_task"): 1,
            ("chat", "_stream_hermes_chat", "asyncio.create_task"): 1,
            ("voice", "_resolve_voice_honcho_binding", "asyncio.to_thread"): 1,
            ("voice", "_resolve_voice_memory_scope", "asyncio.to_thread"): 1,
            ("voice", "heartbeat_voice_canary_session", "asyncio.create_task"): 1,
            ("voice", "get_voice_context", "asyncio.create_task"): 6,
            ("callbacks", "update_conversation_summary", "background_tasks.add_task"): 1,
        }
    )
    chat_source = (_BACKEND / "ella" / "routers" / "chat.py").read_text(encoding="utf-8")
    assert "task.result()" in chat_source
    trace_source = (_BACKEND / "ella" / "routers" / "trace.py").read_text(encoding="utf-8")
    assert "create_task" not in trace_source
    assert "INSERT INTO routing_traces" not in trace_source
