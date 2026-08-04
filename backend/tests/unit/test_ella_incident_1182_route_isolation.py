import ast
import asyncio
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from ella.routers import canonical_events, chat, resolve, trace
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
    headers = {"X-Ella-Event-Ledger-Key": "ledger-service-test"}

    accepted = client.post(
        "/v1/ella/events",
        headers=headers,
        json={"events": [_event(), _event("uid-b", event_id="event-b")]},
    )
    assert accepted.status_code == 200
    assert len(store.writes) == 1

    malformed = _event(event_id="event-c")
    malformed["canonical_identity"] = "uid-b"
    rejected = client.post("/v1/ella/events", headers=headers, json={"events": [malformed]})
    assert rejected.status_code == 403
    assert len(store.writes) == 1

    timeline = client.get("/v1/ella/timeline?uid=uid-b", headers=headers)
    assert timeline.status_code == 200
    assert store.timeline_calls[0][0] == "uid-b"

    completion = client.post(
        "/v1/ella/sessions/session-b/complete",
        headers=headers,
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
    trace._traces.clear()
    private = trace.RouteTrace()
    private.uid = "uid-a"
    private.endpoint = "/v1/ella/chat/stream"
    private.resolved_gateway = "http://private-gateway.invalid"
    private.resolved_session_key = "private-session"
    private.client_headers = {"authorization": "private-token"}
    trace._traces.appendleft(private)
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
    serialized = own.text
    assert "private-gateway" not in serialized
    assert "private-session" not in serialized
    assert "private-token" not in serialized


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
    assert len(recorded) == 1
    assert recorded[0].uid == "uid-a"
    assert recorded[0].resolved_gateway == ""
    assert recorded[0].resolved_session_key == ""
    assert recorded[0].client_headers == {}
    assert recorded[0].notes == ["client-telemetry"]


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


def _decorated_routes(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    routes = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not isinstance(decorator.func, ast.Attribute):
                continue
            if decorator.func.attr not in {"get", "post", "put", "patch", "delete"} or not decorator.args:
                continue
            route = decorator.args[0]
            if not isinstance(route, ast.Constant) or not isinstance(route.value, str):
                continue
            has_dependency = any(
                isinstance(default, ast.Call) and isinstance(default.func, ast.Name) and default.func.id == "Depends"
                for default in [*node.args.defaults, *node.args.kw_defaults]
                if default is not None
            )
            has_dependency = has_dependency or any(
                keyword.arg == "dependencies"
                and any(
                    isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == "Depends"
                    for item in (keyword.value.elts if isinstance(keyword.value, (ast.List, ast.Tuple)) else [])
                )
                for keyword in decorator.keywords
            )
            has_dependency = has_dependency or any(
                isinstance(call.func, ast.Name) and call.func.id == "authenticate_voice_proxy_request"
                for call in ast.walk(node)
                if isinstance(call, ast.Call)
            )
            routes.append((decorator.func.attr.upper(), route.value, node.name, has_dependency))
    return routes


def test_affected_router_inventory_has_no_unclassified_authority_boundary():
    modules = {
        "canonical_events.py": set(),
        "callbacks.py": {("GET", "/health"), ("GET", "/caregiver-dashboard-data")},
        "trace.py": set(),
        "debug_metadata.py": set(),
        "chat.py": set(),
        "resolve.py": set(),
        "voice.py": {("GET", "/config")},
    }
    inventory = []
    for module_name, approved_public in modules.items():
        routes = _decorated_routes(_BACKEND / "ella" / "routers" / module_name)
        assert routes, module_name
        inventory.extend((module_name, *route) for route in routes)
        for method, path, function_name, has_dependency in routes:
            assert has_dependency or (method, path) in approved_public, (
                module_name,
                method,
                path,
                function_name,
            )
    assert len(inventory) >= 40
