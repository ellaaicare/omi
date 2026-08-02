import asyncio
import ast
import json
import urllib.request
from pathlib import Path

import pytest

from ella.utils import auto_provision, identity_sync
from ella.utils.provision_authority import (
    ProvisionAuthorityError,
    hermes_provision_authority,
    legacy_provision_authority,
)


BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _configure_distinct_authorities(monkeypatch):
    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://legacy-authority:8200")
    monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", "legacy-test-token")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", "http://hermes-authority:8210")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", "hermes-test-token")


def test_authority_pairs_use_exact_matching_environment_slots(monkeypatch):
    _configure_distinct_authorities(monkeypatch)

    legacy = legacy_provision_authority()
    hermes = hermes_provision_authority()

    assert (legacy.base_url, legacy.token_reference, legacy.token) == (
        "http://legacy-authority:8200",
        "ELLA_PROVISION_API_TOKEN",
        "legacy-test-token",
    )
    assert (hermes.base_url, hermes.token_reference, hermes.token) == (
        "http://hermes-authority:8210",
        "ELLA_HERMES_PROVISION_API_TOKEN",
        "hermes-test-token",
    )


@pytest.mark.parametrize("failure", ["missing_hermes_token", "shared_url", "shared_token"])
def test_hermes_authority_pair_mismatch_fails_closed(monkeypatch, failure):
    _configure_distinct_authorities(monkeypatch)
    if failure == "missing_hermes_token":
        monkeypatch.delenv("ELLA_HERMES_PROVISION_API_TOKEN")
    elif failure == "shared_url":
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", "http://legacy-authority:8200")
    else:
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", "legacy-test-token")

    with pytest.raises(ProvisionAuthorityError) as exc_info:
        hermes_provision_authority()

    assert exc_info.value.code in {
        "hermes_provision_authority_incomplete",
        "provision_authority_pair_conflict",
    }


def test_new_user_auto_provision_posts_only_to_hermes_authority(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"mode": "hermes_only", "runtimeBinding": {"agentId": "invited-agent"}}

    class AsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(auto_provision.httpx, "AsyncClient", AsyncClient)

    result = asyncio.run(
        auto_provision._provision_with_payload(
            "invited-uid",
            {"userId": "invited-user", "omiUid": "invited-uid", "label": "Invited User"},
        )
    )

    assert result["success"] is True
    assert captured["url"] == "http://hermes-authority:8210/provision"
    assert captured["headers"] == {"Authorization": "Bearer hermes-test-token"}
    assert "legacy-test-token" not in captured["headers"].values()


def test_new_user_auto_provision_rejects_cross_slot_token_before_http(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", "legacy-test-token")

    class ForbiddenAsyncClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("cross-slot token must fail before HTTP")

    monkeypatch.setattr(auto_provision.httpx, "AsyncClient", ForbiddenAsyncClient)

    result = asyncio.run(
        auto_provision._provision_with_payload(
            "invited-uid",
            {"userId": "invited-user", "omiUid": "invited-uid", "label": "Invited User"},
        )
    )

    assert result == {"success": False, "error": "provision_authority_pair_conflict"}


def test_sync_identity_posts_only_to_hermes_authority(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def read():
            return json.dumps({"status": "ok", "changed": True}).encode("utf-8")

    def urlopen(request: urllib.request.Request, timeout: int):
        captured.update(
            url=request.full_url,
            authorization=request.get_header("Authorization"),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(identity_sync.urllib.request, "urlopen", urlopen)

    result = identity_sync.sync_user_identity("invited-uid", email="invited@example.test")

    assert result["status"] == "ok"
    assert captured == {
        "url": "http://hermes-authority:8210/identity-sync",
        "authorization": "Bearer hermes-test-token",
        "timeout": 15,
    }


def test_async_identity_sync_posts_only_to_hermes_authority(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok", "changed": True}

    class AsyncClient:
        def __init__(self, timeout):
            captured["timeout"] = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return Response()

    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", AsyncClient)

    result = asyncio.run(identity_sync.async_sync_user_identity("invited-uid", phone="+15555550100"))

    assert result["status"] == "ok"
    assert captured["url"] == "http://hermes-authority:8210/identity-sync"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": "Bearer hermes-test-token",
    }


def test_retained_history_consumers_remain_on_legacy_authority_pair():
    expected_names = {
        "ella/routers/chat.py": ("ella_chat_history", {"PROVISION_API_URL", "PROVISION_API_TOKEN"}),
        "ella/routers/guardian.py": (
            "_get_recent_chat_turns",
            {"_PROVISION_API_URL", "_PROVISION_API_TOKEN"},
        ),
    }

    for relative_path, (function_name, required_names) in expected_names.items():
        tree = ast.parse((BACKEND_ROOT / relative_path).read_text(encoding="utf-8"))
        function = next(
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
        )
        referenced_names = {node.id for node in ast.walk(function) if isinstance(node, ast.Name)}
        assert required_names <= referenced_names
        assert not {name for name in referenced_names if "HERMES_PROVISION_API" in name}
