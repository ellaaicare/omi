import asyncio
import ast
import json
import logging
import socket
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
    _canonical_base_url,
    hermes_provision_authority,
    legacy_provision_authority,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
LEGACY_URL = "http://100.76.138.56:8200"
HERMES_URL = APPROVED_HERMES_PROVISION_URL
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
    "hostname_destination",
    "loopback_destination",
    "link_local_destination",
    "metadata_destination",
    "private_destination",
    "unspecified_destination",
    "multicast_destination",
    "reserved_destination",
    "short_ipv4_destination",
    "integer_ipv4_destination",
    "octal_ipv4_destination",
    "hex_ipv4_destination",
    "mapped_ipv6_destination",
    "uppercase_scheme_destination",
    "trailing_slash_destination",
    "surrounding_whitespace_destination",
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
            "hostname_destination": "http://localhost:8210",
            "loopback_destination": "http://127.0.0.1:8210",
            "link_local_destination": "http://169.254.10.20:8210",
            "metadata_destination": "http://169.254.169.254:8210",
            "private_destination": "http://10.0.0.1:8210",
            "unspecified_destination": "http://0.0.0.0:8210",
            "multicast_destination": "http://224.0.0.1:8210",
            "reserved_destination": "http://240.0.0.1:8210",
            "short_ipv4_destination": "http://127.1:8210",
            "integer_ipv4_destination": "http://2130706433:8210",
            "octal_ipv4_destination": "http://0177.0.0.1:8210",
            "hex_ipv4_destination": "http://0x7f000001:8210",
            "mapped_ipv6_destination": "http://[::ffff:127.0.0.1]:8210",
            "uppercase_scheme_destination": "HTTP://100.76.138.56:8210",
            "trailing_slash_destination": "http://100.76.138.56:8210/",
            "surrounding_whitespace_destination": " http://100.76.138.56:8210 ",
        }
        destination = destinations[case]
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", destination)
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", destination)
        try:
            binding_destination = _canonical_base_url(
                destination,
                error_code="test_destination_rejected",
            )
        except ProvisionAuthorityError:
            binding_destination = destination.rstrip("/")
        monkeypatch.setenv(BINDING_ENV, _authority_binding_value(binding_destination, HERMES_TOKEN))


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
        (APPROVED_HERMES_PROVISION_URL, "", APPROVED_HERMES_PROVISION_URL),
        (APPROVED_HERMES_PROVISION_URL, APPROVED_HERMES_PROVISION_URL, APPROVED_HERMES_PROVISION_URL),
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
        def __init__(self, timeout, *, follow_redirects, trust_env):
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects
            captured["trust_env"] = trust_env

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
        def __init__(self, timeout, *, follow_redirects, trust_env):
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects
            captured["trust_env"] = trust_env

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
        def __init__(self, timeout, *, follow_redirects, trust_env):
            captured["timeout"] = timeout
            captured["follow_redirects"] = follow_redirects
            captured["trust_env"] = trust_env

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
        def __init__(self, timeout, *, follow_redirects, trust_env):
            self.timeout = timeout
            assert follow_redirects is False
            assert trust_env is False

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


DRIFT_CASES = ("url", "token", "binding_secret", "binding_reference", "destination_policy")


def _drift_authority(monkeypatch, case):
    if case == "url":
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", "http://100.76.138.56:8211")
    elif case == "token":
        replacement = "rotated-hermes-token-secret"
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TOKEN", replacement)
        monkeypatch.setenv(BINDING_ENV, _authority_binding_value(APPROVED_HERMES_PROVISION_URL, replacement))
    elif case == "binding_secret":
        monkeypatch.setenv(BINDING_ENV, "sha256:" + ("0" * 64))
    elif case == "binding_reference":
        replacement_ref = "ELLA_TEST_ROTATED_HERMES_AUTHORITY_BINDING"
        monkeypatch.setenv(replacement_ref, _authority_binding_value(APPROVED_HERMES_PROVISION_URL, HERMES_TOKEN))
        monkeypatch.setenv("ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF", f"env:{replacement_ref}")
    elif case == "destination_policy":
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", "")
    else:  # pragma: no cover - the parametrization is closed
        raise AssertionError(case)


def _runtime_receipt():
    profile = "omi-boundary-user-secret"
    return {
        "mode": "hermes_only",
        "provisionMode": "hermes_only",
        "runtimeBinding": {
            "provider": "hermes",
            "profileName": profile,
            "agentId": "hermes",
            "workspaceRoot": f"/Users/ellaai/.hermes/profiles/{profile}/workspace",
            "internalGatewayUrl": "http://100.76.138.56:8701",
            "gatewayPort": 8701,
            "serviceLabel": f"com.ella.hermes.{profile}",
            "credentialRef": "env:ELLA_HERMES_GATEWAY_KEY_BOUNDARY",
            "healthState": "healthy",
            "smokePassed": True,
            "healthReceipt": {"smoke_passed": True, "probe": "synthetic"},
            "templateVersion": "hermes-user-v1",
            "honcho": {
                "workspace": "honcho-boundary",
                "observedPeer": "boundary",
                "observerPeer": "ella-boundary",
            },
        },
    }


class _RecordingRepository:
    def __init__(self, *, binding=None):
        self.calls = []
        self.binding = binding
        self.job = {
            "id": "11111111-1111-1111-1111-111111111111",
            "target_schema_version": "hermes-user-v1",
            "state": "pending",
            "stage": "identity_ready",
            "retryable": True,
        }
        self.staged = None

    def _record(self, name):
        self.calls.append(name)

    async def assert_schema_ready(self):
        self._record("assert_schema_ready")

    async def assert_self_hosted_invite_schema_ready(self):
        self._record("assert_self_hosted_invite_schema_ready")

    async def get_self_hosted_invitation_admission(self, _uid):
        self._record("get_self_hosted_invitation_admission")
        return {
            "omi_uid": _uid,
            "consent_policy_version": provisioning.CURRENT_POLICY_VERSION,
            "consent_processor_set_hash": provisioning.CURRENT_PROCESSOR_SET_HASH,
            "consent_scope_version": provisioning.CURRENT_SCOPE_VERSION,
            "consent_scope_hash": provisioning.CURRENT_SCOPE_HASH,
            "provider_allowlist": [provisioning.SELF_HOSTED_RUNTIME_PROVIDER],
            "model_allowlist": [provisioning.SELF_HOSTED_RUNTIME_MODEL],
            "mode_allowlist": list(provisioning.SELF_HOSTED_RUNTIME_TARGET_MODES),
            "fallback_policy": {"enabled": False, "order": []},
        }

    async def ensure_user_identity(self, **_kwargs):
        self._record("ensure_user_identity")

    async def acquire_job(self, **_kwargs):
        self._record("acquire_job")
        return dict(self.job)

    async def ensure_omi_user_document(self, **_kwargs):
        self._record("ensure_omi_user_document")

    async def resolve_active_runtime(self, *_args, **_kwargs):
        self._record("resolve_active_runtime")
        return self.binding

    async def claim_job(self, _job_id):
        self._record("claim_job")
        self.job.update(state="provisioning", stage="profile_ready")
        return dict(self.job)

    async def stage_runtime_binding(self, *, uid, binding):
        self._record("stage_runtime_binding")
        self.staged = {**binding, "omi_uid": uid, "revision": 1}
        return self.staged

    async def update_job(self, **kwargs):
        self._record("update_job")
        self.job.update(kwargs)
        return dict(self.job)

    async def activate_runtime_binding(self, **_kwargs):
        self._record("activate_runtime_binding")
        return {**self.staged, "revision": 2, "active": True}

    async def activate_user(self, _uid):
        self._record("activate_user")


class _DriftBeforeCoordinator(ProvisioningCoordinator):
    def __init__(self, *args, monkeypatch, drift_case, target, occurrence=1, **kwargs):
        super().__init__(*args, **kwargs)
        self._monkeypatch = monkeypatch
        self._drift_case = drift_case
        self._target = target
        self._occurrence = occurrence
        self._operation_counts = {}

    async def _repository_call(self, authority_snapshot, operation, *args, **kwargs):
        name = operation.__name__
        self._operation_counts[name] = self._operation_counts.get(name, 0) + 1
        if name == self._target and self._operation_counts[name] == self._occurrence:
            _drift_authority(self._monkeypatch, self._drift_case)
        return await super()._repository_call(authority_snapshot, operation, *args, **kwargs)


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_real_provision_client_revalidates_after_client_entry_before_send(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    sends = []

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            _drift_authority(monkeypatch, case)
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            sends.append("http")
            raise AssertionError("drift must stop the transport send")

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", AsyncClient)

    with pytest.raises(ProvisioningError):
        asyncio.run(HermesProvisionClient().provision(_identity(), "hermes-user-v1"))

    assert sends == []


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_real_claimed_worker_stops_after_inflight_http_authority_drift(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    sends = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _runtime_receipt()

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            sends.append("http")
            _drift_authority(monkeypatch, case)
            return Response()

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", AsyncClient)
    repository = _RecordingRepository()

    try:
        asyncio.run(
            ProvisioningCoordinator(repository).process_claimed_job(
                job=repository.job,
                identity=_identity(),
            )
        )
    except ProvisioningError:
        pass

    assert sends == ["http"]
    assert repository.calls == []


@pytest.mark.parametrize("case", DRIFT_CASES)
@pytest.mark.parametrize(
    ("target", "occurrence", "forbidden"),
    [
        ("stage_runtime_binding", 1, {"stage_runtime_binding", "update_job", "activate_runtime_binding"}),
        ("update_job", 1, {"update_job", "activate_runtime_binding"}),
        ("activate_runtime_binding", 1, {"activate_runtime_binding"}),
        ("update_job", 2, set()),
    ],
)
def test_claimed_worker_rechecks_before_every_post_http_mutation(
    monkeypatch,
    case,
    target,
    occurrence,
    forbidden,
):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return _runtime_receipt()

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            return Response()

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", AsyncClient)
    repository = _RecordingRepository()
    coordinator = _DriftBeforeCoordinator(
        repository,
        monkeypatch=monkeypatch,
        drift_case=case,
        target=target,
        occurrence=occurrence,
    )

    try:
        asyncio.run(coordinator.process_claimed_job(job=repository.job, identity=_identity()))
    except ProvisioningError:
        pass

    if target == "update_job" and occurrence == 2:
        assert repository.calls.count("update_job") == 1
        assert repository.calls[-1] == "activate_runtime_binding"
    else:
        assert forbidden.isdisjoint(repository.calls)


@pytest.mark.parametrize("case", DRIFT_CASES)
@pytest.mark.parametrize(
    "target",
    (
        "assert_schema_ready",
        "assert_self_hosted_invite_schema_ready",
        "get_self_hosted_invitation_admission",
        "ensure_user_identity",
        "acquire_job",
        "ensure_omi_user_document",
        "resolve_active_runtime",
        "claim_job",
    ),
)
def test_coordinator_rechecks_before_each_schema_identity_firebase_and_job_boundary(monkeypatch, case, target):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    repository = _RecordingRepository()
    coordinator = _DriftBeforeCoordinator(
        repository,
        monkeypatch=monkeypatch,
        drift_case=case,
        target=target,
    )

    with pytest.raises(ProvisioningError):
        asyncio.run(
            coordinator.ensure_job(
                identity=_identity(),
                target_schema_version="hermes-user-v1",
                client_request_id="boundary-request",
                request_payload={"client": "test"},
            )
        )

    assert target not in repository.calls


@pytest.mark.parametrize("case", DRIFT_CASES)
@pytest.mark.parametrize("target", ("activate_user", "update_job"))
def test_coordinator_rechecks_before_existing_binding_activation_and_job_receipt(monkeypatch, case, target):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    repository = _RecordingRepository(
        binding={
            "revision": 7,
            "user_status": "PENDING",
            "active": True,
            "template_version": "hermes-user-v1",
        }
    )
    coordinator = _DriftBeforeCoordinator(
        repository,
        monkeypatch=monkeypatch,
        drift_case=case,
        target=target,
    )

    with pytest.raises(ProvisioningError):
        asyncio.run(
            coordinator.ensure_job(
                identity=_identity(),
                target_schema_version="hermes-user-v1",
                client_request_id="boundary-request",
                request_payload={"client": "test"},
            )
        )

    assert target not in repository.calls


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_auto_provision_revalidates_after_user_lookup_before_http(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []

    class Pool:
        async def fetchrow(self, *_args, **_kwargs):
            _drift_authority(monkeypatch, case)
            return {
                "email": "boundary-email-secret@example.test",
                "name": "Boundary",
                "identities": '{}',
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
            }

        async def execute(self, *_args, **_kwargs):
            side_effects.append("database")

    async def pool():
        return Pool()

    class ForbiddenAsyncClient:
        def __init__(self, *_args, **_kwargs):
            side_effects.append("http")
            raise AssertionError("drift must stop HTTP")

    monkeypatch.setattr(auto_provision, "_get_pool", pool)
    monkeypatch.setattr(auto_provision.httpx, "AsyncClient", ForbiddenAsyncClient)

    result = asyncio.run(auto_provision.auto_provision_user("boundary-user-secret"))

    assert result["success"] is False
    assert side_effects == []


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_auto_provision_stops_database_write_after_inflight_http_drift(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []

    class Pool:
        async def fetchrow(self, *_args, **_kwargs):
            return {
                "email": "boundary-email-secret@example.test",
                "name": "Boundary",
                "identities": json.dumps({"phone": "+15555550100"}),
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
            }

        async def execute(self, *_args, **_kwargs):
            side_effects.append("database")

    async def pool():
        return Pool()

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"mode": "hermes_only"}

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            side_effects.append("http")
            _drift_authority(monkeypatch, case)
            return Response()

    monkeypatch.setattr(auto_provision, "_get_pool", pool)
    monkeypatch.setattr(auto_provision.httpx, "AsyncClient", AsyncClient)

    result = asyncio.run(auto_provision.auto_provision_user("boundary-user-secret"))

    assert result["success"] is False
    assert side_effects == ["http"]


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_auto_provision_rechecks_after_send_helper_before_identity_write(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []

    class Pool:
        async def fetchrow(self, *_args, **_kwargs):
            return {
                "email": "boundary-email-secret@example.test",
                "name": "Boundary",
                "identities": json.dumps({"phone": "+15555550100"}),
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
            }

        async def execute(self, *_args, **_kwargs):
            side_effects.append("database")

    async def pool():
        return Pool()

    async def provisioned(_uid, _payload, *, authority_snapshot):
        assert "<opaque>" in repr(authority_snapshot)
        side_effects.append("http")
        _drift_authority(monkeypatch, case)
        return {"success": True, "cluster": {}}

    monkeypatch.setattr(auto_provision, "_get_pool", pool)
    monkeypatch.setattr(auto_provision, "_provision_with_payload", provisioned)

    result = asyncio.run(auto_provision.auto_provision_user("boundary-user-secret"))

    assert result["success"] is False
    assert side_effects == ["http"]


@pytest.mark.parametrize("entrypoint", ("sync", "async"))
@pytest.mark.parametrize("case", DRIFT_CASES)
def test_identity_sync_revalidates_after_payload_boundary_before_send(monkeypatch, entrypoint, case):
    _configure_distinct_authorities(monkeypatch)
    sends = []
    original_payload = identity_sync._identity_payload

    def drifting_payload(*args, **kwargs):
        _drift_authority(monkeypatch, case)
        return original_payload(*args, **kwargs)

    def forbidden_open(*_args, **_kwargs):
        sends.append("sync-http")
        raise AssertionError("drift must stop HTTP")

    class ForbiddenAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            sends.append("async-http")
            raise AssertionError("drift must stop HTTP")

    monkeypatch.setattr(identity_sync, "_identity_payload", drifting_payload)
    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", forbidden_open)
    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", ForbiddenAsyncClient)

    if entrypoint == "sync":
        result = identity_sync.sync_user_identity("boundary-user-secret", email="boundary-email-secret@example.test")
    else:
        result = asyncio.run(
            identity_sync.async_sync_user_identity(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
        )

    assert result["status"] == "error"
    assert sends == []


@pytest.mark.parametrize(
    "entrypoint",
    ("sync", "async_httpx", "async_fallback", "fire_and_forget", "reconcile"),
)
@pytest.mark.parametrize("case", DRIFT_CASES)
def test_identity_sync_entrypoints_revalidate_after_request_construction_before_transport(
    monkeypatch,
    entrypoint,
    case,
):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []
    boundary = {"payload_built": False, "post_payload_checks": 0, "drifted": False}
    original_payload = identity_sync._identity_payload
    original_authority = identity_sync.hermes_provision_authority

    def tracked_payload(*args, **kwargs):
        payload = original_payload(*args, **kwargs)
        boundary["payload_built"] = True
        return payload

    def authority_with_construction_boundary(expected_snapshot=None):
        if expected_snapshot is not None and boundary["payload_built"] and not boundary["drifted"]:
            boundary["post_payload_checks"] += 1
            if boundary["post_payload_checks"] == 2:
                boundary["drifted"] = True
                _drift_authority(monkeypatch, case)
        return original_authority(expected_snapshot)

    def forbidden_open(*_args, **_kwargs):
        side_effects.append("sync-http")
        raise AssertionError("post-construction drift must stop urllib transport")

    class ForbiddenAsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            side_effects.append("async-http")
            raise AssertionError("post-construction drift must stop HTTPX transport")

    class ImmediateThread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target

        def start(self):
            side_effects.append("thread-executed")
            self.target()

    monkeypatch.setattr(identity_sync, "_identity_payload", tracked_payload)
    monkeypatch.setattr(identity_sync, "hermes_provision_authority", authority_with_construction_boundary)
    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", forbidden_open)

    if entrypoint == "sync":
        result = identity_sync.sync_user_identity(
            "boundary-user-secret",
            email="boundary-email-secret@example.test",
        )
        assert result["status"] == "error"
    elif entrypoint == "async_httpx":
        monkeypatch.setattr(identity_sync.httpx, "AsyncClient", ForbiddenAsyncClient)
        result = asyncio.run(
            identity_sync.async_sync_user_identity(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
        )
        assert result["status"] == "error"
    elif entrypoint == "async_fallback":
        monkeypatch.setattr(identity_sync, "httpx", None)
        result = asyncio.run(
            identity_sync.async_sync_user_identity(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
        )
        assert result["status"] == "error"
    elif entrypoint == "fire_and_forget":
        monkeypatch.setattr(identity_sync.threading, "Thread", ImmediateThread)
        identity_sync.sync_user_identity_fire_and_forget(
            "boundary-user-secret",
            email="boundary-email-secret@example.test",
        )
    else:
        monkeypatch.setattr(identity_sync, "_load_state", lambda: {"last_check": "2026-01-01T00:00:00Z"})
        monkeypatch.setattr(
            identity_sync,
            "_get_changed_users",
            lambda _last_check: [
                {
                    "omi_uid": "boundary-user-secret",
                    "email": "boundary-email-secret@example.test",
                    "identities": {},
                    "updated_at": "2026-01-02T00:00:00Z",
                }
            ],
        )
        monkeypatch.setattr(identity_sync, "_save_state", lambda _state: side_effects.append("state"))
        identity_sync.reconcile()

    assert boundary == {"payload_built": True, "post_payload_checks": 2, "drifted": True}
    assert side_effects == (["thread-executed"] if entrypoint == "fire_and_forget" else [])


@pytest.mark.parametrize("entrypoint", ("sync", "async"))
@pytest.mark.parametrize("case", DRIFT_CASES)
def test_identity_sync_detects_inflight_http_drift_before_later_effects(monkeypatch, entrypoint, case):
    _configure_distinct_authorities(monkeypatch)
    sends = []

    class SyncResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            sends.append("sync-http")
            _drift_authority(monkeypatch, case)
            return b'{"status":"ok"}'

    def open_request(*_args, **_kwargs):
        return SyncResponse()

    class AsyncResponse:
        status_code = 200

        @staticmethod
        def raise_for_status():
            return None

        @staticmethod
        def json():
            return {"status": "ok"}

    class AsyncClient:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, *_args, **_kwargs):
            sends.append("async-http")
            _drift_authority(monkeypatch, case)
            return AsyncResponse()

    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", open_request)
    monkeypatch.setattr(identity_sync.httpx, "AsyncClient", AsyncClient)

    if entrypoint == "sync":
        result = identity_sync.sync_user_identity("boundary-user-secret", email="boundary-email-secret@example.test")
        assert sends == ["sync-http"]
    else:
        result = asyncio.run(
            identity_sync.async_sync_user_identity(
                "boundary-user-secret",
                email="boundary-email-secret@example.test",
            )
        )
        assert sends == ["async-http"]

    assert result["status"] == "error"


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_fire_and_forget_rechecks_after_thread_construction_before_execution(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []

    class Thread:
        def __init__(self, *, target, daemon):
            assert daemon is True
            self.target = target
            _drift_authority(monkeypatch, case)

        def start(self):
            side_effects.append("thread-start")
            self.target()

    monkeypatch.setattr(identity_sync.threading, "Thread", Thread)
    monkeypatch.setattr(
        identity_sync._IDENTITY_SYNC_OPENER,
        "open",
        lambda *_args, **_kwargs: side_effects.append("http"),
    )

    identity_sync.sync_user_identity_fire_and_forget(
        "boundary-user-secret",
        email="boundary-email-secret@example.test",
    )

    assert side_effects == []


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_reconciliation_rechecks_after_database_batch_and_before_state_or_http(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []
    monkeypatch.setattr(identity_sync, "_load_state", lambda: {"last_check": "2026-01-01T00:00:00Z"})

    def changed_users(_last_check):
        _drift_authority(monkeypatch, case)
        return [
            {
                "omi_uid": "boundary-user-secret",
                "email": "boundary-email-secret@example.test",
                "identities": {},
                "updated_at": "2026-01-02T00:00:00Z",
            }
        ]

    monkeypatch.setattr(identity_sync, "_get_changed_users", changed_users)
    monkeypatch.setattr(identity_sync, "_save_state", lambda _state: side_effects.append("state"))
    monkeypatch.setattr(
        identity_sync._IDENTITY_SYNC_OPENER,
        "open",
        lambda *_args, **_kwargs: side_effects.append("http"),
    )

    identity_sync.reconcile()

    assert side_effects == []


@pytest.mark.parametrize("case", DRIFT_CASES)
def test_reconciliation_stops_batch_and_state_after_first_inflight_drift(monkeypatch, case):
    _configure_distinct_authorities(monkeypatch)
    side_effects = []
    monkeypatch.setattr(identity_sync, "_load_state", lambda: {"last_check": "2026-01-01T00:00:00Z"})
    monkeypatch.setattr(
        identity_sync,
        "_get_changed_users",
        lambda _last_check: [
            {
                "omi_uid": "boundary-user-one",
                "email": "boundary-one@example.test",
                "identities": {},
                "updated_at": "2026-01-02T00:00:00Z",
            },
            {
                "omi_uid": "boundary-user-two",
                "email": "boundary-two@example.test",
                "identities": {},
                "updated_at": "2026-01-03T00:00:00Z",
            },
        ],
    )
    monkeypatch.setattr(identity_sync, "_save_state", lambda _state: side_effects.append("state"))

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self):
            side_effects.append("http")
            _drift_authority(monkeypatch, case)
            return b'{"status":"ok"}'

    monkeypatch.setattr(identity_sync._IDENTITY_SYNC_OPENER, "open", lambda *_args, **_kwargs: Response())

    identity_sync.reconcile()

    assert side_effects == ["http"]


def test_legacy_mapped_ipv6_socket_equivalence_collides(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://[::ffff:100.76.138.56]:8210")

    with pytest.raises(ProvisionAuthorityError, match="provision_authority_pair_conflict"):
        hermes_provision_authority()


def test_hostnames_fail_closed_before_multiple_answer_or_rebinding_dns(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    resolutions = []

    def forbidden_dns(*_args, **_kwargs):
        resolutions.append("dns")
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.76.138.57", 8210)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.76.138.56", 8210)),
        ]

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://legacy.example.test:8210")

    with pytest.raises(ProvisionAuthorityError, match="legacy_provision_authority_incomplete"):
        hermes_provision_authority()

    monkeypatch.setenv("ELLA_PROVISION_API_URL", LEGACY_URL)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_URL", "http://hermes.example.test:8210")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_REVIEWED_ALLOWLIST", "http://hermes.example.test:8210")
    monkeypatch.setenv(
        BINDING_ENV,
        _authority_binding_value("http://hermes.example.test:8210", HERMES_TOKEN),
    )
    with pytest.raises(ProvisionAuthorityError, match="hermes_provision_authority_destination_rejected"):
        hermes_provision_authority()

    assert resolutions == []


def test_http_transports_disable_redirects_proxies_and_environment_drift(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    captured = {}

    class Response:
        status_code = 302

    class AsyncClient:
        def __init__(self, timeout, *, follow_redirects, trust_env):
            captured.update(timeout=timeout, follow_redirects=follow_redirects, trust_env=trust_env)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def post(self, url, **_kwargs):
            captured["url"] = url
            return Response()

    monkeypatch.setattr(provisioning.httpx, "AsyncClient", AsyncClient)

    with pytest.raises(ProvisioningError, match="provision_request_rejected"):
        asyncio.run(HermesProvisionClient().provision(_identity(), "hermes-user-v1"))

    assert captured["follow_redirects"] is False
    assert captured["trust_env"] is False
    assert captured["url"] == f"{APPROVED_HERMES_PROVISION_URL}/provision"
    assert identity_sync._NoRedirectHandler().redirect_request(None, None, 302, "redirect", {}, "http://evil") is None
    proxy_handlers = [
        handler
        for handler in identity_sync._IDENTITY_SYNC_OPENER.handlers
        if isinstance(handler, identity_sync.urllib.request.ProxyHandler)
    ]
    assert proxy_handlers == []


def test_authority_snapshot_and_authority_repr_are_non_loggable(monkeypatch):
    _configure_distinct_authorities(monkeypatch)
    authority = hermes_provision_authority()
    rendered = f"{authority!r} {authority.snapshot()!r}"

    for forbidden in (HERMES_TOKEN, LEGACY_TOKEN, BINDING_ENV, "sha256:"):
        assert forbidden not in rendered
