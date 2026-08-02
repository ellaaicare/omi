import asyncio
import ast
import json
import logging
from pathlib import Path

import pytest

from ella.services import provisioning
from ella.services.provisioning import (
    HermesProvisionClient,
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
)
from ella.utils import auto_provision, identity_sync
from ella.utils.provision_authority import (
    APPROVED_HERMES_PROVISION_URL,
    ProvisionAuthorityError,
    _authority_binding_value,
    hermes_provision_authority,
    legacy_provision_authority,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
LEGACY_URL = "http://legacy-authority:8200"
HERMES_URL = "http://hermes-authority:8210"
LEGACY_TOKEN = "legacy-test-token-distinct"
HERMES_TOKEN = "hermes-test-token-distinct"
BINDING_ENV = "ELLA_TEST_HERMES_PROVISION_AUTHORITY_BINDING"

REJECTION_CASES = (
    "missing_url",
    "missing_token",
    "missing_binding_reference",
    "missing_binding_secret",
    "binding_mismatch",
    "exact_url_collapse",
    "canonical_url_collapse",
    "one_way_url_swap",
    "two_way_url_swap",
    "one_way_token_swap",
    "two_way_token_swap",
    "same_endpoint_different_token",
    "different_endpoint_same_token",
    "malformed_destination",
    "userinfo_destination",
    "path_destination",
    "query_destination",
    "fragment_destination",
    "loopback_destination",
    "link_local_destination",
    "metadata_destination",
    "private_destination",
)


def _configure_distinct_authorities(monkeypatch, *, hermes_url=HERMES_URL, hermes_token=HERMES_TOKEN):
    monkeypatch.setenv("ELLA_PROVISION_API_URL", LEGACY_URL)
    monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", LEGACY_TOKEN)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", hermes_url)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", hermes_token)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", hermes_url)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF", f"env:{BINDING_ENV}")
    monkeypatch.setenv(BINDING_ENV, _authority_binding_value(hermes_url.rstrip("/"), hermes_token))


def _apply_rejection_case(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    if case == "missing_url":
        monkeypatch.delenv("ELLA_HERMES_PROVISION_API_URL")
    elif case == "missing_token":
        monkeypatch.delenv("ELLA_HERMES_PROVISION_API_TOKEN")
    elif case == "missing_binding_reference":
        monkeypatch.delenv("ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF")
    elif case == "missing_binding_secret":
        monkeypatch.delenv(BINDING_ENV)
    elif case == "binding_mismatch":
        monkeypatch.setenv(BINDING_ENV, "sha256:" + ("0" * 64))
    elif case in {"exact_url_collapse", "one_way_url_swap", "same_endpoint_different_token"}:
        monkeypatch.setenv("ELLA_PROVISION_API_URL", HERMES_URL)
    elif case == "canonical_url_collapse":
        canonical_url = "https://hermes-authority"
        monkeypatch.setenv("ELLA_PROVISION_API_URL", "HTTPS://HERMES-AUTHORITY:443/")
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", canonical_url)
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", canonical_url)
        monkeypatch.setenv(BINDING_ENV, _authority_binding_value(canonical_url, HERMES_TOKEN))
    elif case == "two_way_url_swap":
        monkeypatch.setenv("ELLA_PROVISION_API_URL", HERMES_URL)
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", LEGACY_URL)
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", f"{HERMES_URL},{LEGACY_URL}")
    elif case == "one_way_token_swap":
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", LEGACY_TOKEN)
    elif case == "two_way_token_swap":
        monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", HERMES_TOKEN)
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", LEGACY_TOKEN)
    elif case == "different_endpoint_same_token":
        monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", HERMES_TOKEN)
    else:
        destinations = {
            "malformed_destination": "not-a-url",
            "userinfo_destination": "http://operator@100.76.138.56:8210",
            "path_destination": f"{APPROVED_HERMES_PROVISION_URL}/admin",
            "query_destination": f"{APPROVED_HERMES_PROVISION_URL}?target=other",
            "fragment_destination": f"{APPROVED_HERMES_PROVISION_URL}#other",
            "loopback_destination": "http://127.0.0.1:8210",
            "link_local_destination": "http://169.254.10.20:8210",
            "metadata_destination": "http://169.254.169.254:8210",
            "private_destination": "http://10.0.0.1:8210",
        }
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", destinations[case])


def _identity():
    return VerifiedIdentity(
        uid="boundary-user-secret",
        email="boundary-email-secret@example.test",
        name="Boundary User",
        timezone="UTC",
    )


def test_authority_pairs_use_exact_matching_environment_slots(monkeypatch):
    _configure_distinct_authorities(monkeypatch)

    legacy = legacy_provision_authority()
    hermes = hermes_provision_authority()

    assert (legacy.base_url, legacy.token_reference, legacy.token) == (
        LEGACY_URL,
        "ELLA_PROVISION_API_TOKEN",
        LEGACY_TOKEN,
    )
    assert (hermes.base_url, hermes.token_reference, hermes.token, hermes.binding_reference) == (
        HERMES_URL,
        "ELLA_HERMES_PROVISION_API_TOKEN",
        HERMES_TOKEN,
        f"env:{BINDING_ENV}",
    )
    assert HERMES_TOKEN not in repr(hermes)


@pytest.mark.parametrize("case", REJECTION_CASES)
def test_hermes_authority_rejection_matrix_fails_closed(monkeypatch, case):
    _apply_rejection_case(monkeypatch, case)

    with pytest.raises(ProvisionAuthorityError) as exc_info:
        hermes_provision_authority()

    assert exc_info.value.code in {
        "hermes_provision_authority_incomplete",
        "hermes_provision_authority_destination_rejected",
        "hermes_provision_authority_binding_invalid",
        "provision_authority_pair_conflict",
    }


@pytest.mark.parametrize(
    ("configured", "allowlist", "canonical"),
    [
        ("HTTP://100.76.138.56:8210/", "", APPROVED_HERMES_PROVISION_URL),
        ("HTTPS://HERMES.EXAMPLE.TEST:443/", "https://hermes.example.test", "https://hermes.example.test"),
    ],
)
def test_hermes_authority_accepts_exact_or_reviewed_canonical_endpoint(
    monkeypatch,
    configured,
    allowlist,
    canonical,
):
    _configure_distinct_authorities(monkeypatch, hermes_url=configured)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", allowlist)
    monkeypatch.setenv(BINDING_ENV, _authority_binding_value(canonical, HERMES_TOKEN))

    authority = hermes_provision_authority()

    assert authority.base_url == canonical


@pytest.mark.parametrize("case", REJECTION_CASES)
def test_real_provision_client_and_coordinator_reject_before_http_or_repository_side_effect(
    monkeypatch,
    case,
):
    _apply_rejection_case(monkeypatch, case)
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", "")
    http_calls = []
    repository_calls = []

    class ForbiddenAsyncClient:
        def __init__(self, *args, **kwargs):
            http_calls.append((args, kwargs))
            raise AssertionError("authority rejection must precede HTTP")

    class ForbiddenRepository:
        def __getattr__(self, name):
            async def forbidden(*args, **kwargs):
                repository_calls.append((name, args, kwargs))
                raise AssertionError("authority rejection must precede repository access")

            return forbidden

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", ForbiddenAsyncClient)
    client = HermesProvisionClient()
    coordinator = ProvisioningCoordinator(ForbiddenRepository())

    with pytest.raises(ProvisioningError):
        asyncio.run(client.provision(_identity(), "hermes-user-v1"))
    with pytest.raises(ProvisioningError):
        asyncio.run(
            coordinator.ensure_job(
                identity=_identity(),
                target_schema_version="hermes-user-v1",
                client_request_id="boundary-request",
                request_payload={"client": "test"},
            )
        )
    with pytest.raises(ProvisioningError):
        asyncio.run(
            coordinator.process_claimed_job(
                job={"id": "boundary-job", "target_schema_version": "hermes-user-v1"},
                identity=_identity(),
            )
        )

    assert http_calls == []
    assert repository_calls == []


def test_real_provision_client_posts_only_to_accepted_exact_authority(monkeypatch):
    _configure_distinct_authorities(monkeypatch, hermes_url=APPROVED_HERMES_PROVISION_URL)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", "")
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"mode": "hermes_only", "provisionMode": "hermes_only"}

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

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", AsyncClient)

    result = asyncio.run(HermesProvisionClient().provision(_identity(), "hermes-user-v1"))

    assert result == {"mode": "hermes_only", "provisionMode": "hermes_only"}
    assert captured["url"] == f"{APPROVED_HERMES_PROVISION_URL}/provision"
    assert captured["headers"] == {"Authorization": f"Bearer {HERMES_TOKEN}"}


@pytest.mark.parametrize("case", REJECTION_CASES)
def test_auto_provision_entrypoint_rejects_before_database_or_http(monkeypatch, case):
    _apply_rejection_case(monkeypatch, case)
    side_effects = []

    async def forbidden_pool():
        side_effects.append("database")
        raise AssertionError("authority rejection must precede database access")

    class ForbiddenAsyncClient:
        def __init__(self, *args, **kwargs):
            side_effects.append("http")
            raise AssertionError("authority rejection must precede HTTP")

    monkeypatch.setattr(auto_provision, "_get_pool", forbidden_pool)
    monkeypatch.setattr(auto_provision.httpx, "AsyncClient", ForbiddenAsyncClient)

    result = asyncio.run(auto_provision.auto_provision_user("boundary-user-secret"))

    assert result["success"] is False
    assert side_effects == []


@pytest.mark.parametrize("entrypoint", ["sync", "async", "fire_and_forget", "reconcile"])
@pytest.mark.parametrize("case", REJECTION_CASES)
def test_identity_sync_entrypoints_reject_before_http_database_state_or_thread(
    monkeypatch,
    entrypoint,
    case,
):
    _apply_rejection_case(monkeypatch, case)
    side_effects = []

    def forbidden_open(*args, **kwargs):
        side_effects.append(("http", args, kwargs))
        raise AssertionError("authority rejection must precede HTTP")

    class ForbiddenAsyncClient:
        def __init__(self, *args, **kwargs):
            side_effects.append(("http", args, kwargs))
            raise AssertionError("authority rejection must precede HTTP")

    class ForbiddenThread:
        def __init__(self, *args, **kwargs):
            side_effects.append(("thread", args, kwargs))
            raise AssertionError("authority rejection must precede thread creation")

    def forbidden_state(*args, **kwargs):
        side_effects.append(("state", args, kwargs))
        raise AssertionError("authority rejection must precede state or database access")

    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", forbidden_open)
    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", ForbiddenAsyncClient)
    monkeypatch.setattr(identity_sync.threading, "Thread", ForbiddenThread)
    monkeypatch.setattr(identity_sync, "_load_state", forbidden_state)
    monkeypatch.setattr(identity_sync, "_get_changed_users", forbidden_state)
    monkeypatch.setattr(identity_sync, "_save_state", forbidden_state)

    if entrypoint == "sync":
        result = identity_sync.sync_user_identity(
            "boundary-user-secret",
            email="boundary-email-secret@example.test",
        )
        assert result["status"] == "error"
    elif entrypoint == "async":
        result = asyncio.run(
            identity_sync.async_sync_user_identity(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
        )
        assert result["status"] == "error"
    elif entrypoint == "fire_and_forget":
        assert (
            identity_sync.sync_user_identity_fire_and_forget(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
            is None
        )
    else:
        assert identity_sync.reconcile() is None

    assert side_effects == []


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
    assert captured["url"] == f"{HERMES_URL}/provision"
    assert captured["headers"] == {"Authorization": f"Bearer {HERMES_TOKEN}"}
    assert LEGACY_TOKEN not in captured["headers"].values()


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

    def open_request(request, timeout):
        captured.update(
            url=request.full_url,
            authorization=request.get_header("Authorization"),
            timeout=timeout,
        )
        return Response()

    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", open_request)

    result = identity_sync.sync_user_identity("invited-uid", email="invited@example.test")

    assert result["status"] == "ok"
    assert captured == {
        "url": f"{HERMES_URL}/identity-sync",
        "authorization": f"Bearer {HERMES_TOKEN}",
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
    assert captured["url"] == f"{HERMES_URL}/identity-sync"
    assert captured["headers"] == {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {HERMES_TOKEN}",
    }


@pytest.mark.parametrize("entrypoint", ["sync", "async", "fire_and_forget", "reconcile"])
def test_identity_sync_entrypoints_accept_exact_approved_endpoint(monkeypatch, entrypoint):
    _configure_distinct_authorities(monkeypatch, hermes_url=APPROVED_HERMES_PROVISION_URL)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", "")
    captured = []

    class SyncResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        @staticmethod
        def read():
            return b'{"status":"ok","changed":true}'

    def open_request(request, timeout):
        captured.append((request.full_url, request.get_header("Authorization"), timeout))
        return SyncResponse()

    class AsyncResponse:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok", "changed": True}

    class AsyncClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, traceback):
            return False

        async def post(self, url, *, headers, json):
            captured.append((url, headers["Authorization"], self.timeout))
            return AsyncResponse()

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            self.target()

    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", open_request)
    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", AsyncClient)
    monkeypatch.setattr(identity_sync.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(identity_sync, "_load_state", lambda: {"last_check": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr(
        identity_sync,
        "_get_changed_users",
        lambda _last_check: [
            {
                "omi_uid": "invited-uid",
                "email": "invited@example.test",
                "identities": {},
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ],
    )
    monkeypatch.setattr(identity_sync, "_save_state", lambda _state: None)

    if entrypoint == "sync":
        assert identity_sync.sync_user_identity("invited-uid", email="invited@example.test")["status"] == "ok"
    elif entrypoint == "async":
        result = asyncio.run(identity_sync.async_sync_user_identity("invited-uid", email="invited@example.test"))
        assert result["status"] == "ok"
    elif entrypoint == "fire_and_forget":
        identity_sync.sync_user_identity_fire_and_forget("invited-uid", email="invited@example.test")
    else:
        identity_sync.reconcile()

    assert captured == [
        (
            f"{APPROVED_HERMES_PROVISION_URL}/identity-sync",
            f"Bearer {HERMES_TOKEN}",
            15 if entrypoint != "async" else 15.0,
        )
    ]


def test_authority_errors_and_logs_are_code_only(monkeypatch, caplog):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv(BINDING_ENV, "sha256:" + ("0" * 64))
    caplog.set_level(logging.DEBUG)

    result = identity_sync.sync_user_identity(
        "boundary-user-secret",
        email="boundary-email-secret@example.test",
    )

    assert result == {"status": "error", "error": "hermes_provision_authority_binding_invalid"}
    logged = caplog.text
    for forbidden in (
        "boundary-user-secret",
        "boundary-email-secret@example.test",
        HERMES_TOKEN,
        LEGACY_TOKEN,
        "sha256:",
    ):
        assert forbidden not in logged


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
