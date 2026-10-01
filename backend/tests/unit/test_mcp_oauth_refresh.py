import asyncio
import base64
import copy
import hashlib
import types
import uuid
from datetime import datetime, timedelta, timezone
from dataclasses import replace
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi import HTTPException

from database.mcp_oauth_refresh import RefreshAuthoritySnapshot, _revision
from database import mcp_oauth_refresh as storage
from ella.services import ai_consent, mcp_oauth_refresh as refresh
from ella.routers import mcp_well_known
from ella.services.mcp_identity import (
    ExternalConnectorIdentity,
    MCPIdentityResolution,
    MCPProfileGrant,
    STATE_AUTHENTICATED_MAPPED,
    issue_mcp_session_token,
    validate_mcp_session_token,
)
from test_ella_mcp_onboarding import _client, _load_module
from test_ella_plato_mcp import _client as _plato_client, _summary_test_session


class MemoryRepository:
    """Deterministic service tests; real lock/rotation proof is in the PG suite."""

    def __init__(self):
        self.rows = {}
        self.tokens = {}
        self.lock = asyncio.Lock()
        self.rotations = 0
        self.fail_rotation = False

    async def read(self, family_id):
        row = copy.deepcopy(self.rows.get(family_id))
        if row:
            row["usable"] = row["revoked_at"] is None and row["expires_at"] > datetime.now(timezone.utc)
        return row

    async def find(self, digest):
        value = self.tokens.get(digest)
        if not value:
            return None
        row = await self.read(value[0])
        row["consumed_at"] = value[1]
        return row

    async def create_family(self, authority, digest, token_digest):
        family_id = str(uuid.uuid4())
        row = dict(
            id=family_id,
            authority=copy.deepcopy(authority),
            authority_digest=digest,
            client_id=authority["client_id"],
            expires_at=datetime.now(timezone.utc) + timedelta(days=7),
            revoked_at=None,
        )
        self.rows[family_id] = row
        self.tokens[token_digest] = (family_id, None)
        return copy.deepcopy(row)

    async def revoke(self, family_id):
        self.rows[family_id]["revoked_at"] = datetime.now(timezone.utc)

    async def rotate(self, old_digest, new_digest, *, client_id, authority_digest):
        self.rotations += 1
        if self.fail_rotation:
            raise RuntimeError("synthetic ambiguous commit")
        async with self.lock:
            row = await self.find(old_digest)
            if not row or row["client_id"] != client_id or not row["usable"]:
                raise ValueError("invalid_grant")
            if row["consumed_at"] is not None or row["authority_digest"] != authority_digest:
                await self.revoke(row["id"])
                raise ValueError("invalid_grant")
            self.tokens[old_digest] = (row["id"], datetime.now(timezone.utc))
            self.tokens[new_digest] = (row["id"], None)
            return row


@pytest.fixture
def setup(monkeypatch):
    monkeypatch.setenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "true")
    monkeypatch.setenv("ELLA_MCP_SESSION_SECRET", "synthetic-refresh-signing-secret-32-bytes")
    monkeypatch.setitem(
        refresh.registered_clients,
        "client",
        dict(
            client_id="client",
            redirect_uris=["https://client.test/callback"],
            grant_types=["authorization_code", "refresh_token"],
            response_types=["code"],
            token_endpoint_auth_method="none",
        ),
    )
    repository = MemoryRepository()

    async def create():
        return repository

    monkeypatch.setattr(refresh.MCPRefreshRepository, "create", create)
    consent_repository = ai_consent.InMemoryConsentRepository()
    ai_consent.AiConsentService(consent_repository).submit(
        "owner",
        ai_consent.ConsentSubmission(
            decision="granted",
            request_id="refresh-fixture-request",
            policy_version=ai_consent.CURRENT_POLICY_VERSION,
            processor_set_hash=ai_consent.CURRENT_PROCESSOR_SET_HASH,
            scope_version=ai_consent.CURRENT_SCOPE_VERSION,
            scope_hash=ai_consent.CURRENT_SCOPE_HASH,
            app_version="1",
            build_number="1",
            locale="en",
        ),
    )
    state, receipt = consent_repository.get_current("owner")
    grant = dict(
        grant_id="grant",
        provider_subject="google:subject",
        profile_uid="owner",
        role="self",
        status="active",
        scopes=["tools:read", "memory:read", "summaries:write"],
        allowed_tools=sorted(refresh.SUMMARY_OPERATION_TOOLS),
        metadata=dict(runtime_binding_id="binding", profile_user_id="22222222-2222-4222-8222-222222222222"),
    )
    current = {"snapshot": RefreshAuthoritySnapshot(grant, state, receipt, ("1:1", "1:2", "1:3")), "runtime": "e" * 64}
    monkeypatch.setattr(refresh.FirestoreConsentRepository, "_configured_db", staticmethod(lambda: object()))
    monkeypatch.setattr(refresh, "read_refresh_authority", lambda *a, **kw: current["snapshot"])
    runtime = types.SimpleNamespace(
        binding_id="binding",
        account_user_id="11111111-1111-4111-8111-111111111111",
        profile_user_id=grant["metadata"]["profile_user_id"],
        allowed_tools=tuple(refresh.SUMMARY_OPERATION_TOOLS),
    )

    async def require(uid):
        assert uid == "owner"
        return runtime

    monkeypatch.setattr(refresh, "require_summary_runtime", require)
    monkeypatch.setattr(
        refresh, "runtime_authority_identity", lambda value: types.SimpleNamespace(digest=current["runtime"])
    )
    resolution = MCPIdentityResolution(
        state=STATE_AUTHENTICATED_MAPPED,
        trace_id="trace",
        identity=ExternalConnectorIdentity(provider="google", subject="subject"),
        selected_grant=MCPProfileGrant.from_mapping(grant),
    )
    return repository, current, resolution


async def _issue(setup):
    return await refresh.issue_refresh_family(
        setup[2], client_id="client", scope="tools:read memory:read summaries:write offline_access", ttl_seconds=3600
    )


async def _renew(token, **kwargs):
    return await refresh.renew_refresh_family(
        token=token,
        client_id=kwargs.pop("client_id", "client"),
        scope=kwargs.pop("scope", ""),
        ttl_seconds=3600,
        **kwargs
    )


def test_default_off_no_database_or_refresh_metadata(monkeypatch):
    monkeypatch.delenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", raising=False)
    assert refresh.refresh_enabled() is False
    with pytest.raises(ValueError, match="unsupported_grant_type"):
        asyncio.run(refresh.renew_refresh_family(token="x" * 43, client_id="client", scope="", ttl_seconds=3600))
    module = _load_module(monkeypatch)
    result = _client(module).post("/v1/ella/mcp/token", data={"grant_type": "refresh_token", "refresh_token": "x" * 43})
    assert result.status_code == 400
    assert result.json() == {"error": "unsupported_grant_type"}


def test_rotates_hashed_single_use_credentials_and_caps_expiry(setup):
    async def run():
        first = await _issue(setup)
        claims = validate_mcp_session_token(first["access_token"])
        row = setup[0].rows[claims["refresh_family_id"]]
        row["expires_at"] = datetime.now(timezone.utc) + timedelta(seconds=20)
        second = await _renew(first["refresh_token"])
        assert second["refresh_token"] != first["refresh_token"]
        assert second["expires_in"] <= 20
        assert validate_mcp_session_token(second["access_token"])["exp"] <= int(row["expires_at"].timestamp())
        assert first["refresh_token"] not in str(setup[0].tokens)
        assert second["refresh_token"] not in str(setup[0].rows)
        assert setup[0].tokens[refresh.token_digest(first["refresh_token"])][1] is not None

    asyncio.run(run())


def test_replay_burns_family_and_all_renewable_access(setup):
    async def run():
        first = await _issue(setup)
        second = await _renew(first["refresh_token"])
        with pytest.raises(ValueError):
            await _renew(first["refresh_token"])
        for issued in (first, second):
            with pytest.raises(ValueError):
                await refresh.validate_renewable_session(validate_mcp_session_token(issued["access_token"]))

    asyncio.run(run())


@pytest.mark.parametrize(
    "kind",
    ["grant_aba", "consent_aba", "receipt_aba", "runtime_aba", "scope", "profile", "expired_grant", "consent_declined"],
)
def test_authority_change_burns_refresh_and_old_access(setup, kind):
    async def run():
        issued = await _issue(setup)
        snapshot = copy.deepcopy(setup[1]["snapshot"])
        if kind.endswith("_aba"):
            if kind == "runtime_aba":
                setup[1]["runtime"] = "f" * 64
            else:
                index = {"grant_aba": 0, "consent_aba": 1, "receipt_aba": 2}[kind]
                revisions = list(snapshot.revisions)
                revisions[index] = "1:4"
                snapshot = RefreshAuthoritySnapshot(snapshot.grant, snapshot.state, snapshot.receipt, tuple(revisions))
        elif kind == "scope":
            snapshot.grant["scopes"].remove("summaries:write")
        elif kind == "profile":
            snapshot.grant["metadata"]["profile_user_id"] = "other-profile"
        elif kind == "expired_grant":
            snapshot.grant["expires_at"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        else:
            snapshot.state["decision"] = "declined"
        setup[1]["snapshot"] = snapshot
        with pytest.raises(ValueError):
            await _renew(issued["refresh_token"])
        with pytest.raises(ValueError):
            await refresh.validate_renewable_session(validate_mcp_session_token(issued["access_token"]))

    asyncio.run(run())


def test_unknown_client_restart_scope_expansion_and_other_owner_denied(setup, monkeypatch):
    async def run():
        issued = await _issue(setup)
        with pytest.raises(ValueError):
            await _renew(issued["refresh_token"], client_id="other")
        with pytest.raises(ValueError, match="invalid_scope"):
            await _renew(issued["refresh_token"], scope="tools:read memory:read summaries:write profile:read")
        claims = validate_mcp_session_token(issued["access_token"])
        claims["profile_uid"] = "other"
        with pytest.raises(ValueError):
            await refresh.validate_renewable_session(claims)
        monkeypatch.delitem(refresh.registered_clients, "client")
        with pytest.raises(ValueError):
            await _renew(issued["refresh_token"])

    asyncio.run(run())


def test_ambiguous_rotation_has_no_automatic_replay(setup):
    async def run():
        issued = await _issue(setup)
        setup[0].fail_rotation = True
        with pytest.raises(RuntimeError):
            await _renew(issued["refresh_token"])
        assert setup[0].rotations == 1

    asyncio.run(run())


def test_postcommit_authority_drift_discards_response_and_burns_family(setup, monkeypatch):
    async def run():
        issued = await _issue(setup)
        rotate = setup[0].rotate

        async def changed(*a, **kw):
            result = await rotate(*a, **kw)
            setup[1]["runtime"] = "f" * 64
            return result

        monkeypatch.setattr(setup[0], "rotate", changed)
        with pytest.raises(ValueError):
            await _renew(issued["refresh_token"])
        assert all(row["revoked_at"] is not None for row in setup[0].rows.values())

    asyncio.run(run())


def test_final_authority_read_failure_does_not_return_token(setup, monkeypatch):
    async def run():
        issued = await _issue(setup)
        rotate = setup[0].rotate

        async def unavailable(*a, **kw):
            result = await rotate(*a, **kw)
            monkeypatch.setattr(
                refresh, "read_refresh_authority", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("offline"))
            )
            return result

        monkeypatch.setattr(setup[0], "rotate", unavailable)
        with pytest.raises(RuntimeError):
            await _renew(issued["refresh_token"])
        assert all(row["revoked_at"] is not None for row in setup[0].rows.values())

    asyncio.run(run())


def test_authority_snapshot_revision_preserves_nanos_and_requires_server_revision():
    stamp = types.SimpleNamespace(timestamp_pb=lambda: types.SimpleNamespace(seconds=10, nanos=123456789))
    assert _revision(types.SimpleNamespace(exists=True, update_time=stamp)) == "10:123456789"
    with pytest.raises(ValueError):
        _revision(types.SimpleNamespace(exists=False))


def test_s256_code_exact_registered_client_redirect_and_offline_marker(setup, monkeypatch):
    module = _load_module(monkeypatch)
    monkeypatch.setattr(module, "resolve_oauth_connector_resolution", lambda *a, **kw: setup[2])
    client = _client(module)
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = dict(
        response_type="code",
        client_id="client",
        redirect_uri="https://client.test/callback",
        code_challenge=challenge,
        code_challenge_method="S256",
        firebase_id_token="synthetic",
        scope="tools:read memory:read summaries:write offline_access",
    )
    response = client.get("/v1/ella/mcp/authorize", params=params, follow_redirects=False)
    assert response.status_code == 302
    code = parse_qs(urlparse(response.headers["location"]).query)["code"][0]
    result = client.post(
        "/v1/ella/mcp/token",
        data=dict(
            grant_type="authorization_code",
            client_id="client",
            redirect_uri=params["redirect_uri"],
            code=code,
            code_verifier=verifier,
        ),
    )
    assert result.status_code == 200
    assert "refresh_token" in result.json()
    for changes in (
        {"client_id": "https://unregistered.test"},
        {"redirect_uri": "https://other.test"},
        {"code_challenge_method": "plain"},
    ):
        assert client.get("/v1/ella/mcp/authorize", params={**params, **changes}).status_code == 400


@pytest.mark.parametrize(
    "verifier", ["\U0001f512" * 43, "\u00e9" * 43, "v" * 42, "v" * 129], ids=["utf", "nonascii", "short", "long"]
)
def test_offline_code_invalid_verifier_shape_is_clean_400(setup, monkeypatch, caplog, verifier):
    module = _load_module(monkeypatch)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = module._store_authorization_code(
        setup[2],
        challenge,
        "S256",
        dict(
            client_id="client",
            redirect_uri="https://client.test/callback",
            scope="tools:read memory:read offline_access",
        ),
    )
    response = _client(module).post(
        "/v1/ella/mcp/token",
        data=dict(
            grant_type="authorization_code",
            client_id="client",
            redirect_uri="https://client.test/callback",
            code=code,
            code_verifier=verifier,
        ),
    )
    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid code_verifier (PKCE)"}
    assert verifier not in response.text
    assert verifier not in caplog.text
    assert not setup[0].rows
    assert code not in module._auth_codes


@pytest.mark.parametrize("binding", ["client_id", "redirect_uri", "code_verifier"])
def test_offline_code_wrong_binding_is_consumed_without_family_creation(setup, monkeypatch, binding):
    module = _load_module(monkeypatch)
    verifier = "v" * 43
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    code = module._store_authorization_code(
        setup[2],
        challenge,
        "S256",
        dict(
            client_id="client",
            redirect_uri="https://client.test/callback",
            scope="tools:read memory:read offline_access",
        ),
    )
    body = dict(
        grant_type="authorization_code",
        client_id="client",
        redirect_uri="https://client.test/callback",
        code=code,
        code_verifier=verifier,
    )
    body[binding] = "ella-mcp-test" if binding == "client_id" else "wrong"
    assert _client(module).post("/v1/ella/mcp/token", data=body).status_code == 400
    assert not setup[0].rows
    assert code not in module._auth_codes


@pytest.mark.parametrize("endpoint", ["/mcp", "/mcp/sse/message", "/mcp-get", "/mcp-delete"])
def test_every_protected_plato_transport_rejects_revoked_renewable_access(monkeypatch, endpoint):
    module, auth, _ = _summary_test_session(monkeypatch)
    monkeypatch.setattr(module, "_authenticate", lambda authorization: auth)

    async def deny(claims):
        raise ValueError("invalid_grant")

    monkeypatch.setattr(module, "validate_renewable_session", deny)
    client = _plato_client(module)
    if endpoint == "/mcp-get":
        response = client.get("/v1/ella/plato/mcp")
    elif endpoint == "/mcp-delete":
        response = client.delete("/v1/ella/plato/mcp")
    else:
        response = client.post(
            "/v1/ella/plato" + endpoint, params={"session_id": "missing"}, json={"id": 1, "method": "ping"}
        )
    assert response.status_code == 401


def test_discovery_tool_callback_and_each_batch_message_revalidate(monkeypatch):
    module, auth, _ = _summary_test_session(monkeypatch)
    count = []

    async def checked(claims):
        count.append(claims)

    monkeypatch.setattr(module, "validate_renewable_session", checked)
    assert asyncio.run(module._discovered_tools(auth))
    assert len(count) >= 2
    count.clear()
    asyncio.run(module._handle_mcp_message(auth, {"id": 1, "method": "ping"}))
    asyncio.run(module._handle_mcp_message(auth, {"id": 2, "method": "ping"}))
    assert len(count) == 2

    async def denied(claims):
        raise ValueError("revoked")

    monkeypatch.setattr(module, "validate_renewable_session", denied)
    with pytest.raises(HTTPException):
        asyncio.run(module._require_summary_tool_runtime(auth, "companion_get_conversation_summary"))


@pytest.mark.parametrize("endpoint", ["onboarding", "start_here", "surface-prompt"])
def test_generic_connector_context_rejects_renewable_summary_resource(setup, monkeypatch, endpoint):
    module = _load_module(monkeypatch)
    issued = asyncio.run(_issue(setup))
    result = _client(module).get(
        "/v1/ella/mcp/" + endpoint, headers={"Authorization": "Bearer " + issued["access_token"]}
    )
    assert result.status_code == 401


def test_normal_access_issuer_remains_unchanged_and_renewal_claims_are_bounded(setup):
    token, claims = issue_mcp_session_token(setup[2])
    assert not refresh.RENEWAL_CLAIMS.intersection(claims)
    assert validate_mcp_session_token(token) == claims
    with pytest.raises(ValueError):
        issue_mcp_session_token(setup[2], refresh_family_id=str(uuid.uuid4()))


def test_authorization_code_original_scope_and_tool_ceiling_cannot_expand(setup):
    setup[2].selected_grant.scopes.remove("summaries:write")
    with pytest.raises(ValueError, match="invalid_scope"):
        asyncio.run(_issue(setup))
    assert not setup[0].rows


def test_current_snapshot_changes_during_runtime_await_are_rejected(setup, monkeypatch):
    require = refresh.require_summary_runtime

    async def changed(uid):
        runtime = await require(uid)
        old = setup[1]["snapshot"]
        setup[1]["snapshot"] = RefreshAuthoritySnapshot(old.grant, old.state, old.receipt, ("2:1", "2:2", "2:3"))
        return runtime

    monkeypatch.setattr(refresh, "require_summary_runtime", changed)
    with pytest.raises(ValueError):
        asyncio.run(_issue(setup))
    assert not setup[0].rows


def test_registered_client_replacement_revokes_existing_renewable_access(setup, monkeypatch):
    issued = asyncio.run(_issue(setup))
    client = dict(refresh.registered_clients["client"])
    client["redirect_uris"] = ["https://replacement.test/callback"]
    monkeypatch.setitem(refresh.registered_clients, "client", client)
    with pytest.raises(ValueError):
        asyncio.run(refresh.validate_renewable_session(validate_mcp_session_token(issued["access_token"])))
    assert all(row["revoked_at"] is not None for row in setup[0].rows.values())


def test_read_transaction_uses_same_snapshot_for_grant_pointer_and_receipt(setup):
    marker = object()
    calls = []
    authority = setup[1]["snapshot"]
    stamp = types.SimpleNamespace(timestamp_pb=lambda: types.SimpleNamespace(seconds=10, nanos=123456789))

    class Reference:
        def __init__(self, value):
            self.value = value

        def get(self, *, transaction):
            calls.append(transaction)
            return types.SimpleNamespace(exists=True, update_time=stamp, to_dict=lambda: self.value)

        def collection(self, name):
            assert name == "ai_consent_receipts"
            return self

        def document(self, name):
            assert name == authority.state["receipt_id"]
            return Reference(authority.receipt)

    result = storage._read_authority.to_wrap(
        marker, Reference(authority.grant), Reference({"ai_consent": authority.state})
    )
    assert calls == [marker] * 3
    assert result.revisions == ("10:123456789",) * 3


def test_changed_renewal_jwt_requires_fresh_mcp_initialize(setup, monkeypatch):
    async def run():
        first = await _issue(setup)
        second = await _renew(first["refresh_token"])
        module, _, _ = _summary_test_session(monkeypatch)
        monkeypatch.setattr(module, "validate_mcp_session_token", validate_mcp_session_token)
        client = _plato_client(module)
        started = client.post(
            "/v1/ella/plato/mcp",
            headers={"Authorization": "Bearer " + first["access_token"]},
            json={"id": 1, "method": "initialize"},
        )
        assert started.status_code == 200
        session = started.headers["Mcp-Session-Id"]
        stale = client.post(
            "/v1/ella/plato/mcp",
            headers={"Authorization": "Bearer " + second["access_token"], "Mcp-Session-Id": session},
            json={"id": 2, "method": "ping"},
        )
        assert stale.status_code == 403
        fresh = client.post(
            "/v1/ella/plato/mcp",
            headers={"Authorization": "Bearer " + second["access_token"]},
            json={"id": 3, "method": "initialize"},
        )
        assert fresh.status_code == 200
        assert fresh.headers["Mcp-Session-Id"] != session

    asyncio.run(run())


def test_disabled_feature_and_partial_marker_never_open_storage(setup, monkeypatch):
    monkeypatch.setenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "false")

    async def unavailable():
        raise AssertionError("Default OFF must not open storage")

    monkeypatch.setattr(refresh.MCPRefreshRepository, "create", unavailable)
    asyncio.run(refresh.validate_renewable_session({"sub": "ordinary"}))
    with pytest.raises(ValueError):
        asyncio.run(refresh.validate_renewable_session({"refresh_family_id": str(uuid.uuid4())}))


def test_discovery_metadata_advertises_refresh_only_when_enabled(monkeypatch):
    monkeypatch.setenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "false")
    off = asyncio.run(mcp_well_known.get_oauth_authorization_server())
    assert b'"refresh_token"' not in off.body and b'"offline_access"' not in off.body
    monkeypatch.setenv("ELLA_MCP_OAUTH_REFRESH_ENABLED", "true")
    on = asyncio.run(mcp_well_known.get_oauth_authorization_server())
    assert b'"refresh_token"' in on.body and b'"offline_access"' in on.body


def test_renewal_never_infers_missing_explicit_scope_from_role_defaults(setup):
    setup[1]["snapshot"].grant["scopes"] = []
    with pytest.raises(ValueError):
        asyncio.run(_issue(setup))
    assert not setup[0].rows


@pytest.mark.parametrize("provider", ["static_bearer", "apple", ""])
def test_refresh_requires_exact_durable_external_identity_not_email_fallback(setup, provider):
    resolution = replace(
        setup[2],
        identity=ExternalConnectorIdentity(
            provider=provider, subject="subject", email="synthetic@example.test", email_verified=True
        ),
    )
    with pytest.raises(ValueError):
        asyncio.run(
            refresh.issue_refresh_family(
                resolution, client_id="client", scope="tools:read memory:read offline_access", ttl_seconds=3600
            )
        )
    assert not setup[0].rows


def test_malformed_refresh_error_does_not_echo_or_log_secret(setup, monkeypatch, caplog):
    module = _load_module(monkeypatch)
    private = "synthetic-secret-which-must-not-be-logged"
    response = _client(module).post(
        "/v1/ella/mcp/token", data=dict(grant_type="refresh_token", client_id="client", refresh_token=private)
    )
    assert response.status_code == 400 and response.json() == {"error": "invalid_grant"}
    assert private not in response.text and private not in caplog.text
