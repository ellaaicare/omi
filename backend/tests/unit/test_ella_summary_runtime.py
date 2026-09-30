"""Compose signed summary dispatch with real retained/invitation runtime resolution."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_ella_main_chat_summary_tools import operation, request, run, corrections
from test_ella_plato_mcp import _load_module
from test_ella_provisioning_service import (
    FakeRepository,
    _attestation_challenge,
    _extract,
    _runtime_receipt,
    _self_hosted_admission,
)
from ella.services import mcp_identity, summary_runtime
from ella.services.ai_consent import (
    CURRENT_POLICY_VERSION,
    CURRENT_PROCESSOR_SET_HASH,
    CURRENT_SCOPE_HASH,
    CURRENT_SCOPE_VERSION,
)
from ella.services.runtime_errors import ProvisioningError
from ella.routers import mcp_well_known


@pytest.fixture
def retained_lane(monkeypatch, operation):
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "owner")
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED_UIDS", raising=False)
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    monkeypatch.delenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", raising=False)
    monkeypatch.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", "unit-test-attestation-key-32-bytes-minimum")
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "test-runtime-credential")
    monkeypatch.setenv("ELLA_MCP_SESSION_SECRET", "test-summary-session-signing-secret")
    challenge = _attestation_challenge("owner")
    binding = _extract(_runtime_receipt(challenge=challenge), uid="owner", challenge=challenge)
    binding.update(
        id="44444444-4444-4444-4444-444444444444",
        omi_uid="owner",
        active=True,
        revision=4,
        runtime_target_mode="hermes-chat",
        account_user_id="22222222-2222-2222-2222-222222222222",
        profile_user_id="22222222-2222-2222-2222-222222222222",
        allowed_tools=sorted(mcp_identity.SUMMARY_OPERATION_TOOLS),
    )
    repository = FakeRepository(binding=binding, active_retained=True)

    async def create_repository():
        return repository

    monkeypatch.setattr(summary_runtime.runtime_resolver.EllaProvisioningRepository, "create", create_repository)
    consent = {"active": True}

    def current_consent(uid):
        assert uid == "owner"
        if not consent["active"]:
            raise HTTPException(status_code=403, detail="Current consent required")
        return uid

    monkeypatch.setattr(summary_runtime, "assert_current_ai_consent", current_consent)
    monkeypatch.setattr(corrections, "assert_current_ai_consent", current_consent)
    monkeypatch.setattr(corrections, "require_summary_runtime", summary_runtime.require_summary_runtime)
    monkeypatch.setattr(
        corrections, "revalidate_summary_runtime_authority", summary_runtime.revalidate_summary_runtime_authority
    )
    monkeypatch.setattr(
        corrections, "runtime_authority_identity", summary_runtime.runtime_resolver.runtime_authority_identity
    )
    return SimpleNamespace(repository=repository, binding=binding, consent=consent, operation=operation)


def invitation_lane(monkeypatch, lane):
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "")
    lane.repository.active_retained = False
    lane.repository.self_hosted_owned = True
    lane.repository.self_hosted_admission = _self_hosted_admission("owner")
    lane.binding.update(
        runtime_target_id="target-chat",
        runtime_target_updated_at="2026-09-30T00:00:00Z",
        target_entitlement_revision=2,
        attestation_runtime_target_id="33333333-3333-3333-3333-333333333333",
        target_policy_version=CURRENT_POLICY_VERSION,
        target_processor_set_hash=CURRENT_PROCESSOR_SET_HASH,
        target_scope_version=CURRENT_SCOPE_VERSION,
        target_scope_hash=CURRENT_SCOPE_HASH,
        consent_authority_epoch="11111111-1111-1111-1111-111111111111",
    )


def signed_dispatch(monkeypatch, lane):
    module = _load_module(monkeypatch)
    monkeypatch.setenv("ELLA_MCP_SUMMARY_TOOLS_ENABLED", "true")
    monkeypatch.setenv("ELLA_MCP_SUMMARY_TOOLS_UIDS", "owner")
    monkeypatch.setattr(module, "assert_current_ai_consent", summary_runtime.assert_current_ai_consent)
    monkeypatch.setattr(module, "require_summary_runtime", summary_runtime.require_summary_runtime)
    monkeypatch.setattr(module.summary_tool_registry, "_handler", corrections.run_main_chat_summary_tool)
    monkeypatch.setattr(
        module.conversations_db, "get_conversation", lambda uid, cid: copy.deepcopy(lane.operation.state), raising=False
    )
    grant = mcp_identity.MCPProfileGrant.from_mapping(
        {
            "grant_id": "summary-self-grant",
            "profile_uid": "owner",
            "role": "self",
            "status": "active",
            "scopes": ["tools:read", "memory:read", "summaries:write"],
            "allowed_tools": sorted(mcp_identity.SUMMARY_OPERATION_TOOLS),
            "metadata": {
                "runtime_binding_id": lane.binding["id"],
                "profile_user_id": lane.binding["profile_user_id"],
            },
        }
    )
    grants = [grant]
    monkeypatch.setattr(mcp_identity, "load_identity_grants", lambda identity: grants)
    identity = mcp_identity.ExternalConnectorIdentity(provider="google", subject="summary-owner")
    resolution = mcp_identity.resolve_mcp_identity(identity, grants=grants)
    token, _ = mcp_identity.issue_mcp_session_token(resolution)
    return module, module._authenticate(f"Bearer {token}"), grants


@pytest.mark.parametrize("invitation", [False, True])
def test_real_runtime_lane_signed_discovery_and_write(monkeypatch, retained_lane, invitation):
    if invitation:
        invitation_lane(monkeypatch, retained_lane)
    runtime = asyncio.run(summary_runtime.require_summary_runtime("owner"))
    assert (runtime.provider, runtime.runtime_target_mode) == ("hermes", "hermes-chat")
    module, auth, _ = signed_dispatch(monkeypatch, retained_lane)
    assert {
        tool["name"] for tool in asyncio.run(module._discovered_tools(auth))
    } == mcp_identity.SUMMARY_OPERATION_TOOLS
    result = asyncio.run(
        module._companion_summary_operation(
            "companion_resummarize_conversation",
            {
                "conversation_id": "chosen-memory",
                "expected_active_summary_version_id": "before-v1",
                "idempotency_key": "retained-invocation",
            },
            auth_context=auth,
        )
    )
    assert result["status"] == "applied"
    assert (
        retained_lane.operation.state["transcript_segments"] == retained_lane.operation.original["transcript_segments"]
    )
    config = retained_lane.operation.provider_calls[0]["config"]
    assert config.cloud_authority is None
    assert config.hermes_url == f"{runtime.gateway_url}/v1/chat/completions"
    assert retained_lane.operation.provider_calls[0]["session_key"] == "ella:omi:owner:canonical"


@pytest.mark.parametrize(
    "field,value",
    [
        ("runtime_target_mode", "hermes-voice"),
        ("runtime_target_mode", ""),
        ("provider", "hermes_cloud"),
        ("omi_uid", "other-owner"),
        ("id", ""),
        ("profile_user_id", ""),
        ("account_user_id", ""),
    ],
)
def test_real_runtime_lane_rejects_wrong_observed_authority(monkeypatch, retained_lane, field, value):
    retained_lane.binding[field] = value
    with pytest.raises(ProvisioningError):
        asyncio.run(run(request()))
    assert retained_lane.operation.provider_calls == []
    assert retained_lane.operation.writes == []
    assert retained_lane.operation.audits == {}
    module, auth, _ = signed_dispatch(monkeypatch, retained_lane)
    assert asyncio.run(module._discovered_tools(auth)) == []
    with pytest.raises(ProvisioningError):
        asyncio.run(
            module._companion_summary_operation(
                "companion_resummarize_conversation",
                {
                    "conversation_id": "chosen-memory",
                    "expected_active_summary_version_id": "before-v1",
                    "idempotency_key": "retained-invocation",
                },
                auth_context=auth,
            )
        )
    assert retained_lane.operation.provider_calls == []


def test_real_cloud_authority_is_not_a_summary_lane(monkeypatch, retained_lane):
    monkeypatch.setenv("ELLA_HERMES_CLOUD_PROVISIONING_ENABLED", "true")
    with pytest.raises(ProvisioningError, match="cloud_runtime_target_mode_required"):
        asyncio.run(run(request()))
    assert retained_lane.repository.last_resolution_arguments is None
    assert retained_lane.operation.provider_calls == []
    assert retained_lane.operation.writes == []


@pytest.mark.parametrize("missing", ["tool-policy", "grant", "consent"])
def test_real_runtime_discovery_requires_current_tool_grant_and_consent(monkeypatch, retained_lane, missing):
    module, auth, grants = signed_dispatch(monkeypatch, retained_lane)
    if missing == "tool-policy":
        retained_lane.binding["allowed_tools"] = []
    elif missing == "grant":
        grants.clear()
    else:
        retained_lane.consent["active"] = False
    assert asyncio.run(module._discovered_tools(auth)) == []
    with pytest.raises((module.ToolExecutionError, HTTPException, ProvisioningError)):
        asyncio.run(
            module._companion_summary_operation(
                "companion_resummarize_conversation",
                {
                    "conversation_id": "chosen-memory",
                    "expected_active_summary_version_id": "before-v1",
                    "idempotency_key": "retained-invocation",
                },
                auth_context=auth,
            )
        )
    assert retained_lane.operation.provider_calls == []
    assert retained_lane.operation.writes == []


def test_real_runtime_consent_revoked_during_resolution_is_rejected(retained_lane):
    async def delayed_resolution(uid):
        retained_lane.consent["active"] = False
        return retained_lane.binding

    retained_lane.repository.resolve_self_hosted_active_direct = delayed_resolution
    with pytest.raises(HTTPException, match="Current consent required"):
        asyncio.run(summary_runtime.require_summary_runtime("owner"))


@pytest.mark.parametrize("revoked", ["binding", "mode", "consent", "grant"])
def test_real_runtime_authority_change_during_provider_blocks_publication(monkeypatch, retained_lane, revoked):
    module, auth, grants = signed_dispatch(monkeypatch, retained_lane)
    original_generate = corrections.generate_summary_from_prompt

    async def generate(**kwargs):
        result = await original_generate(**kwargs)
        if revoked == "binding":
            retained_lane.binding["id"] = "55555555-5555-5555-5555-555555555555"
            # Even a newly authorized replacement must not adopt this invocation.
            grants[0].metadata["runtime_binding_id"] = retained_lane.binding["id"]
        elif revoked == "mode":
            retained_lane.binding["runtime_target_mode"] = "hermes-voice"
        elif revoked == "consent":
            retained_lane.consent["active"] = False
        else:
            grants.clear()
        return result

    monkeypatch.setattr(corrections, "generate_summary_from_prompt", generate)
    with pytest.raises((module.ToolExecutionError, HTTPException, ProvisioningError)):
        asyncio.run(
            module._companion_summary_operation(
                "companion_resummarize_conversation",
                {
                    "conversation_id": "chosen-memory",
                    "expected_active_summary_version_id": "before-v1",
                    "idempotency_key": "retained-invocation",
                },
                auth_context=auth,
            )
        )
    assert len(retained_lane.operation.provider_calls) == 1
    assert retained_lane.operation.writes == []
    assert retained_lane.operation.audits == {}


def test_real_runtime_writer_rejects_binding_replacement_without_mcp(monkeypatch, retained_lane):
    original_generate = corrections.generate_summary_from_prompt

    async def generate(**kwargs):
        result = await original_generate(**kwargs)
        retained_lane.binding["revision"] += 1
        return result

    monkeypatch.setattr(corrections, "generate_summary_from_prompt", generate)
    with pytest.raises(ProvisioningError, match="summary_runtime_authority_changed"):
        asyncio.run(run(request()))
    assert retained_lane.operation.writes == []
    assert retained_lane.operation.audits == {}


def test_summary_scope_discovery_preserves_existing_scopes():
    for response in [
        asyncio.run(mcp_well_known.get_oauth_protected_resource()),
        asyncio.run(mcp_well_known.get_oauth_authorization_server()),
    ]:
        scopes = set(json.loads(response.body)["scopes_supported"])
        assert scopes == {
            "context:read",
            "memory:read",
            "summaries:write",
            "observations:write",
            "profile:read",
            "startup:read",
            "timeline:read",
            "tools:read",
            "proposals:read",
            "proposals:write",
        }
