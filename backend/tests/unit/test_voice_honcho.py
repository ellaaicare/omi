import asyncio
from types import SimpleNamespace

import httpx

from ella.services import voice_honcho


def _runtime():
    return SimpleNamespace(
        uid="uid-a",
        honcho_workspace="workspace-a",
        observer_peer="ella-a",
        observed_peer="user-a",
    )


def test_context_uses_exact_receipt_target(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return {
                "peer_id": "ella-a",
                "target_id": "user-a",
                "representation": "User enjoys gardening.",
                "peer_card": ["Prefers morning calls"],
            }

    class Client:
        def __init__(self, *args, **kwargs):
            calls.append(("timeout", kwargs.get("timeout")))

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, url, headers=None, params=None):
            calls.append((url, headers, params))
            return Response()

    monkeypatch.setattr(voice_honcho, "HONCHO_BASE_URL", "http://honcho.test")
    monkeypatch.setattr(voice_honcho, "HONCHO_API_KEY", "honcho-secret")
    monkeypatch.setattr(voice_honcho.httpx, "AsyncClient", Client)

    result = asyncio.run(
        voice_honcho.fetch_voice_honcho_context(
            _runtime(),
            query="gardening and recent plans",
            top_k=7,
        )
    )

    assert result["available"] is True
    assert "User enjoys gardening." in result["context"]
    assert "Prefers morning calls" in result["context"]
    assert calls[1] == (
        "http://honcho.test/v3/workspaces/workspace-a/peers/ella-a/context",
        {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": "Bearer honcho-secret",
        },
        {
            "target": "user-a",
            "search_query": "gardening and recent plans",
            "search_top_k": 7,
            "include_most_frequent": "true",
            "max_conclusions": 7,
        },
    )


def test_context_timeout_degrades_without_raising(monkeypatch):
    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(voice_honcho.httpx, "AsyncClient", Client)

    result = asyncio.run(voice_honcho.fetch_voice_honcho_context(_runtime(), query="recent themes"))

    assert result["available"] is False
    assert result["reason"] == "honcho_timeout"
    assert result["context"] == ""


def test_semantic_search_filters_cross_target_rows(monkeypatch):
    calls = []

    class Response:
        status_code = 200

        def json(self):
            return [
                {
                    "id": "good",
                    "content": "User A discussed tomato plants.",
                    "observer_id": "ella-a",
                    "observed_id": "user-a",
                    "created_at": "2026-07-24T12:00:00Z",
                },
                {
                    "id": "wrong-user",
                    "content": "Private User B fact.",
                    "observer_id": "ella-b",
                    "observed_id": "user-b",
                    "created_at": "2026-07-24T12:01:00Z",
                },
            ]

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, headers=None, json=None):
            calls.append((url, json))
            return Response()

    monkeypatch.setattr(voice_honcho, "HONCHO_BASE_URL", "http://honcho.test")
    monkeypatch.setattr(voice_honcho.httpx, "AsyncClient", Client)

    results = asyncio.run(voice_honcho.search_voice_honcho(_runtime(), "tomato plants", 5))

    assert [item["metadata"]["conclusion_id"] for item in results] == ["good"]
    assert calls == [
        (
            "http://honcho.test/v3/workspaces/workspace-a/conclusions/query",
            {
                "query": "tomato plants",
                "top_k": 5,
                "filters": {"observer": "ella-a", "observed": "user-a"},
            },
        )
    ]
