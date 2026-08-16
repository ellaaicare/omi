import ast
import asyncio
import copy
import os
import re
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import pytest

from database.runtime_targets import (
    SELF_HOSTED_RUNTIME_MODEL,
    SELF_HOSTED_RUNTIME_PROVIDER,
    SELF_HOSTED_RUNTIME_TARGET_MODES,
)
from database.honcho_attestation import (
    ATTESTATION_TTL_SECONDS,
    HonchoAttestationError,
    calculate_signature,
    create_challenge,
    observed_runtime_fields,
    authority_credential,
)
from database.ella_provisioning import (
    EllaProvisioningRepository,
    ProvisioningSchemaNotReadyError,
    deterministic_runtime_binding_id,
)
from ella.services.ai_consent import (
    CURRENT_POLICY_VERSION,
    CURRENT_PROCESSOR_SET_HASH,
    CURRENT_SCOPE_HASH,
    CURRENT_SCOPE_VERSION,
)
from ella.services.provisioning import (
    ATTESTATION_VERIFICATION_GRACE_ENV,
    DEFAULT_ATTESTATION_VERIFICATION_GRACE_SECONDS,
    DEFAULT_PROVISION_TIMEOUT_SECONDS,
    MAX_PROVISION_RESPONSE_BYTES,
    MAX_PROVISION_TIMEOUT_SECONDS,
    HermesProvisionClient,
    ProvisionDeadline,
    ProvisioningCoordinator,
    ProvisioningError,
    VerifiedIdentity,
    extract_runtime_binding,
    provision_idempotency_key,
    provision_timeout_seconds,
    public_receipt,
    provisioning_enabled,
    retained_compatibility_receipt,
    resolve_gateway_credential,
    rollout_enabled,
    self_hosted_fresh_uid_relax_enabled,
    self_hosted_provisioning_enabled,
    stable_payload_hash,
    validate_internal_gateway_url,
    validated_provision_timeout_seconds,
)
from ella.services import provisioning as provisioning_service
from ella.services.runtime_resolver import (
    resolve_isolated_runtime,
    retained_owner_uid_configured,
    runtime_authority_enabled,
    runtime_bindings_enabled,
    runtime_from_binding,
)
from ella.utils.provision_authority import ProvisionAuthority, ProvisionAuthoritySnapshot


def _job(**overrides):
    value = {
        "id": uuid.UUID("11111111-1111-1111-1111-111111111111"),
        "user_id": uuid.UUID("22222222-2222-2222-2222-222222222222"),
        "target_schema_version": "hermes-user-v1",
        "state": "pending",
        "stage": "identity_ready",
        "retryable": True,
    }
    value.update(overrides)
    return value


def _attestation_challenge(uid="user-a"):
    challenge = create_challenge(
        firebase_uid=uid,
        account_owner_id="22222222-2222-2222-2222-222222222222",
        runtime_target_id="33333333-3333-3333-3333-333333333333",
        binding_id="44444444-4444-4444-4444-444444444444",
        job_id="11111111-1111-1111-1111-111111111111",
        now=1_700_000_000,
    )
    challenge["nonce"] = "unit_test_nonce_abcdefghijklmnopqrstuvwxyz012345"
    return challenge


def _attach_attestation(receipt, challenge):
    if "runtimeBinding" not in receipt:
        return receipt
    raw = receipt["runtimeBinding"]
    profile_name = raw["profileName"]
    profiles_root = "/Users/ellaai/.hermes/profiles"
    attestation = {
        **challenge,
        **observed_runtime_fields(
            profile_name=profile_name,
            config_path=f"{profiles_root}/{profile_name}/honcho.json",
            workspace_root=raw["workspaceRoot"],
            honcho_workspace=raw["honcho"]["workspace"],
            observed_peer_id=raw["honcho"]["observedPeer"],
            observer_peer_id=raw["honcho"]["observerPeer"],
            gateway_port=raw["gatewayPort"],
            gateway_target=raw["internalGatewayUrl"],
            credential_ref=raw["credentialRef"],
            agent_id=raw["agentId"],
            service_label=raw["serviceLabel"],
        ),
    }
    attestation["signature"] = calculate_signature(attestation)
    receipt["honchoAttestation"] = attestation
    return receipt


def _runtime_receipt(profile_name="omi-user-a", challenge=None):
    profiles_root = "/Users/ellaai/.hermes/profiles"
    honcho_target = {
        "workspace": "honcho-user-a",
        "observed_peer_id": "user-a",
        "observer_peer_id": "ella-user-a",
        "hermesProfile": profile_name,
    }
    honcho_config_path = f"{profiles_root}/{profile_name}/honcho.json"
    receipt = {
        "mode": "hermes_only",
        "provisionMode": "hermes_only",
        "honchoProfileMap": {
            "status": "ok",
            "honchoConfigPath": honcho_config_path,
            "target": honcho_target,
        },
        "provisioningReceipt": {
            "honcho": {
                "validation": {
                    "ok": True,
                    "mapped": True,
                    "profile": profile_name,
                    "configPath": honcho_config_path,
                    "target": honcho_target,
                }
            }
        },
        "runtimeBinding": {
            "provider": "hermes",
            "profileName": profile_name,
            "agentId": "hermes",
            "workspaceRoot": f"/Users/ellaai/.hermes/profiles/{profile_name}/workspace",
            "internalGatewayUrl": "http://100.76.138.56:8701",
            "gatewayPort": 8701,
            "serviceLabel": f"com.ella.hermes.{profile_name}",
            "credentialRef": "env:ELLA_HERMES_GATEWAY_KEY_USER_A",
            "healthState": "healthy",
            "smokePassed": True,
            "healthReceipt": {"smoke_passed": True, "probe": "synthetic"},
            "templateVersion": "hermes-user-v1",
            "modelPolicyVersion": "frontier-v1",
            "voicePolicyVersion": "ella-voice-v1",
            "honcho": {
                "workspace": "honcho-user-a",
                "observedPeer": "user-a",
                "observerPeer": "ella-user-a",
            },
        },
    }
    return _attach_attestation(receipt, challenge or _attestation_challenge())


def _extract(receipt, uid="user-a", *, challenge=None, expected_template_version=None, now=1_700_000_001):
    return extract_runtime_binding(
        receipt,
        uid,
        expected_attestation_challenge=challenge or _attestation_challenge(uid),
        expected_template_version=expected_template_version,
        now=now,
    )


@pytest.fixture(autouse=True)
def _honcho_attestation_key(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", "unit-test-attestation-key-32-bytes-minimum")


def _self_hosted_admission(uid: str):
    return {
        "omi_uid": uid,
        "user_id": "22222222-2222-2222-2222-222222222222",
        "attestation_runtime_target_id": "33333333-3333-3333-3333-333333333333",
        "consent_policy_version": CURRENT_POLICY_VERSION,
        "consent_processor_set_hash": CURRENT_PROCESSOR_SET_HASH,
        "consent_scope_version": CURRENT_SCOPE_VERSION,
        "consent_scope_hash": CURRENT_SCOPE_HASH,
        "provider_allowlist": [SELF_HOSTED_RUNTIME_PROVIDER],
        "model_allowlist": [SELF_HOSTED_RUNTIME_MODEL],
        "mode_allowlist": list(SELF_HOSTED_RUNTIME_TARGET_MODES),
        "fallback_policy": {"enabled": False, "order": []},
    }


class FakeRepository:
    def __init__(
        self,
        *,
        binding=None,
        omi_identity_error=None,
        schema_error=None,
        self_hosted_admission=None,
        self_hosted_owned=None,
    ):
        self.job = _job()
        self.binding = binding
        self.omi_identity_error = omi_identity_error
        self.schema_error = schema_error
        self.staged = None
        self.identity_calls = []
        self.omi_identity_calls = []
        self.user_active = False
        self.activation_calls = 0
        self.schema_checks = 0
        self.self_hosted_schema_checks = 0
        self.self_hosted_admission = self_hosted_admission
        self.self_hosted_owned = (
            self_hosted_admission is not None if self_hosted_owned is None else bool(self_hosted_owned)
        )
        self.job_calls = []

    async def assert_schema_ready(self):
        self.schema_checks += 1
        if self.schema_error:
            raise self.schema_error

    async def ensure_user_identity(self, **kwargs):
        self.identity_calls.append(kwargs)
        return {"omi_uid": kwargs["uid"]}

    async def acquire_job(self, **kwargs):
        self.job_calls.append(kwargs)
        return dict(self.job)

    async def assert_self_hosted_invite_schema_ready(self):
        self.self_hosted_schema_checks += 1

    async def get_self_hosted_invitation_admission(self, _uid):
        return self.self_hosted_admission

    async def has_invitation_owned_self_hosted_runtime(self, _uid):
        return self.self_hosted_owned

    async def ensure_omi_user_document(self, **kwargs):
        if self.omi_identity_error:
            raise self.omi_identity_error
        self.omi_identity_calls.append(kwargs)
        return True

    async def resolve_active_runtime(self, uid, template_version=None, **_kwargs):
        if self.binding and template_version and self.binding.get("template_version") != template_version:
            return None
        return self.binding

    async def claim_job(self, job_id):
        if self.job["state"] in {"ready", "blocked", "provisioning"}:
            return None
        self.job.update(state="provisioning", stage="profile_ready")
        return dict(self.job)

    async def update_job(self, **kwargs):
        self.job_calls.append(dict(kwargs))
        if self.job["state"] in {"ready", "blocked", "rolling_back", "manual_intervention"} and (
            self.job["state"] != kwargs["state"]
        ):
            return dict(self.job)
        self.job.update(
            state=kwargs["state"],
            stage=kwargs["stage"],
            retryable=kwargs["retryable"],
            error_code=kwargs.get("error_code"),
        )
        return dict(self.job)

    async def prepare_runtime_binding_identity(self, *, uid, provider, role):
        del uid, provider, role
        return "44444444-4444-4444-4444-444444444444"

    async def stage_runtime_binding(self, *, uid, binding):
        self.staged = dict(
            binding,
            id=binding["binding_id"],
            omi_uid=uid,
            account_user_id="22222222-2222-2222-2222-222222222222",
            profile_user_id="22222222-2222-2222-2222-222222222222",
            attestation_runtime_target_id="33333333-3333-3333-3333-333333333333",
            revision=1,
            active=False,
        )
        return self.staged

    async def activate_runtime_binding(
        self,
        *,
        uid,
        provider,
        require_invitation_target=False,
        authority_lineage=None,
        model=SELF_HOSTED_RUNTIME_MODEL,
    ):
        del require_invitation_target, authority_lineage, model
        self.activation_calls += 1
        self.binding = dict(self.staged, active=True, revision=2)
        self.user_active = True
        return self.binding

    async def activate_user(self, uid):
        self.user_active = True


class FakeProvisionClient:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def provision(self, identity, target_schema_version, *, attestation_challenge):
        self.calls.append((identity, target_schema_version, attestation_challenge))
        return _attach_attestation(copy.deepcopy(self.result), attestation_challenge)


def _local_provision_client(base_url: str) -> HermesProvisionClient:
    snapshot = ProvisionAuthoritySnapshot(b"s" * 32)
    authority = ProvisionAuthority(
        base_url=base_url,
        token="synthetic-local-provider-token",
        token_reference="LOCAL_TEST_ONLY",
        _snapshot=snapshot,
    )

    class LocalProvisionClient(HermesProvisionClient):
        @staticmethod
        def resolve_authority(expected_snapshot=None):
            if expected_snapshot is not None and not snapshot.matches(expected_snapshot):
                raise AssertionError("unexpected authority snapshot")
            return authority

    return LocalProvisionClient()


class _FakeDocument:
    def __init__(self, *, exists, data=None):
        self.exists = exists
        self.data = data or {}
        self.writes = []

    def get(self):
        return self

    def to_dict(self):
        return dict(self.data)

    def set(self, payload, *, merge):
        self.writes.append((payload, merge))


class _FakeFirestore:
    def __init__(self, document):
        self.document_ref = document

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, _uid):
        return self.document_ref


class _FakeSchemaPool:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def fetchrow(self, query, *args):
        self.calls.append((query, args))
        return self.result


def test_payload_hash_is_stable_and_sensitive_to_values():
    assert stable_payload_hash({"b": 2, "a": 1}) == stable_payload_hash({"a": 1, "b": 2})
    assert stable_payload_hash({"a": 1}) != stable_payload_hash({"a": 2})


def test_uid_scoped_rollout_is_exact_and_global_flag_still_wins(monkeypatch):
    monkeypatch.setenv("TEST_ROLLOUT_ENABLED", "false")
    monkeypatch.setenv("TEST_ROLLOUT_UIDS", "firebase-A, firebase-B")

    assert rollout_enabled("TEST_ROLLOUT_ENABLED", "TEST_ROLLOUT_UIDS", "firebase-A") is True
    assert rollout_enabled("TEST_ROLLOUT_ENABLED", "TEST_ROLLOUT_UIDS", "firebase-a") is False
    assert rollout_enabled("TEST_ROLLOUT_ENABLED", "TEST_ROLLOUT_UIDS", None) is False

    monkeypatch.setenv("TEST_ROLLOUT_ENABLED", "true")
    assert rollout_enabled("TEST_ROLLOUT_ENABLED", "TEST_ROLLOUT_UIDS", "unlisted") is True


def test_provisioning_and_runtime_canary_allowlists_are_independent(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", "provision-user")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "runtime-user")

    assert provisioning_enabled("provision-user") is True
    assert provisioning_enabled("runtime-user") is False
    assert runtime_bindings_enabled("runtime-user") is True
    assert runtime_bindings_enabled("provision-user") is False


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        (None, 180.0),
        ("120", 120.0),
        ("5", 30.0),
        ("900", 300.0),
        ("not-a-number", 180.0),
        ("nan", 180.0),
        ("inf", 180.0),
    ],
)
def test_provision_timeout_is_bounded_for_cold_runtime_starts(monkeypatch, configured, expected):
    if configured is None:
        monkeypatch.delenv("ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS", configured)

    assert provision_timeout_seconds() == expected


def test_attestation_key_separation_tracks_runtime_credential_lookups_from_source(monkeypatch):
    backend_root = Path(__file__).resolve().parents[2]
    credential_name = re.compile(
        r"^(?!(?:ELLA_HERMES_PROVISION_ATTESTATION_KEY)$)[A-Z][A-Z0-9_]*(?:_KEYS?|_TOKENS?|_SECRET|_PASSWORD)$"
    )

    def literal_environment_names(node):
        if not isinstance(node, ast.Call):
            return ()
        function = node.func
        if isinstance(function, ast.Name) and function.id == "authority_credential":
            candidates = node.args
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "getenv"
            and isinstance(function.value, ast.Name)
            and function.value.id == "os"
        ):
            candidates = node.args[:1]
        elif (
            isinstance(function, ast.Attribute)
            and function.attr == "get"
            and isinstance(function.value, ast.Attribute)
            and function.value.attr == "environ"
            and isinstance(function.value.value, ast.Name)
            and function.value.value.id == "os"
        ):
            candidates = node.args[:1]
        else:
            return ()
        return tuple(
            candidate.value
            for candidate in candidates
            if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str)
        )

    discovered = set()
    for relative_root in ("database", "ella", "routers", "utils/ella"):
        for source_path in (backend_root / relative_root).rglob("*.py"):
            if source_path.name == "honcho_attestation.py":
                continue
            source_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            discovered.update(
                name
                for node in ast.walk(source_tree)
                for name in literal_environment_names(node)
                if credential_name.fullmatch(name)
            )
    assert {
        "ELLA_PROVISION_API_KEY",
        "PROVISION_API_TOKEN",
        "HERMES_VOICE_MEMORY_TOKEN",
        "API_SERVER_KEY",
    } <= discovered

    shared = "synthetic-equal-authority-material-000001"
    candidates = sorted(discovered) + ["ELLA_HERMES_GATEWAY_KEY_TEST_RUNTIME"]
    for candidate in candidates:
        with monkeypatch.context() as scoped:
            scoped.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
            scoped.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", shared)
            scoped.setenv(candidate, shared)
            repository = FakeRepository()
            client = FakeProvisionClient({"mode": "hermes_only", "provisionMode": "hermes_only"})
            asyncio.run(
                ProvisioningCoordinator(repository, client).process_claimed_job(
                    job=_job(state="provisioning", stage="profile_ready"),
                    identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
                )
            )
            assert client.calls == []
            assert repository.staged is None
            assert repository.activation_calls == 0
            assert repository.job["error_code"] == "honcho_attestation_key_conflict"
            assert shared not in str(repository.job)

    with monkeypatch.context() as scoped:
        scoped.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", shared)
        scoped.setenv("ELLA_HERMES_PROVISION_AUTHORITY_BINDING_REF", "env:ELLA_TEST_PROVISION_BINDING")
        scoped.setenv("ELLA_TEST_PROVISION_BINDING", f" {shared} ")
        with pytest.raises(HonchoAttestationError, match="honcho_attestation_key_conflict"):
            create_challenge(
                firebase_uid="user-a",
                account_owner_id="owner-a",
                runtime_target_id="target-a",
                binding_id="binding-a",
                job_id="job-a",
            )

    stale_cached = "synthetic-stale-runtime-authority-000001"
    with monkeypatch.context() as scoped:
        scoped.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", stale_cached)
        scoped.setenv("API_SERVER_KEY", stale_cached)
        assert authority_credential("API_SERVER_KEY") == stale_cached
        scoped.setenv("API_SERVER_KEY", "synthetic-reloaded-runtime-authority-000002")
        with pytest.raises(HonchoAttestationError, match="honcho_attestation_key_conflict"):
            create_challenge(
                firebase_uid="user-a",
                account_owner_id="owner-a",
                runtime_target_id="target-a",
                binding_id="binding-a",
                job_id="job-a",
            )


_RETAINED_AUTHORITY_ENVIRONMENT_NAME = re.compile(
    r"^(?!(?:ELLA_HERMES_PROVISION_ATTESTATION_KEY)$)[A-Z][A-Z0-9_]*(?:_KEYS?|_TOKENS?|_SECRET|_PASSWORD)$"
)


def _environment_expression_kind(node, *, os_aliases, environ_aliases, getenv_aliases):
    if isinstance(node, ast.Name):
        if node.id in os_aliases:
            return "os"
        if node.id in environ_aliases:
            return "environ"
        if node.id in getenv_aliases:
            return "getenv"
        return None
    if isinstance(node, ast.Attribute):
        owner_kind = _environment_expression_kind(
            node.value,
            os_aliases=os_aliases,
            environ_aliases=environ_aliases,
            getenv_aliases=getenv_aliases,
        )
        if owner_kind == "os" and node.attr == "environ":
            return "environ"
        if owner_kind == "os" and node.attr == "getenv":
            return "getenv"
        if owner_kind == "environ" and node.attr == "get":
            return "getenv"
        return None
    if not isinstance(node, ast.Call):
        return None
    if isinstance(node.func, ast.Name) and node.func.id == "__import__" and node.args:
        module_name = node.args[0]
        if isinstance(module_name, ast.Constant) and module_name.value == "os":
            return "os"
    if isinstance(node.func, ast.Attribute) and node.func.attr == "import_module" and node.args:
        module_name = node.args[0]
        if isinstance(module_name, ast.Constant) and module_name.value == "os":
            return "os"
    if not (isinstance(node.func, ast.Name) and node.func.id == "getattr" and len(node.args) >= 2):
        return None
    owner_kind = _environment_expression_kind(
        node.args[0],
        os_aliases=os_aliases,
        environ_aliases=environ_aliases,
        getenv_aliases=getenv_aliases,
    )
    attribute = node.args[1]
    if not isinstance(attribute, ast.Constant) or not isinstance(attribute.value, str):
        return None
    if owner_kind == "os" and attribute.value == "environ":
        return "environ"
    if owner_kind == "os" and attribute.value == "getenv":
        return "getenv"
    if owner_kind == "environ" and attribute.value == "get":
        return "getenv"
    return None


def _assignment_alias_pairs(target, value):
    if isinstance(target, ast.Name):
        return [(target.id, value)]
    if isinstance(target, (ast.Tuple, ast.List)) and isinstance(value, (ast.Tuple, ast.List)):
        pairs = []
        for child_target, child_value in zip(target.elts, value.elts):
            pairs.extend(_assignment_alias_pairs(child_target, child_value))
        return pairs
    return []


def _environment_aliases(source_tree):
    os_aliases = set()
    environ_aliases = set()
    getenv_aliases = set()
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Import):
            os_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "os")
        elif isinstance(node, ast.ImportFrom) and node.module == "os":
            environ_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "environ")
            getenv_aliases.update(alias.asname or alias.name for alias in node.names if alias.name == "getenv")

    assignments = []
    for node in ast.walk(source_tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assignments.extend(_assignment_alias_pairs(target, node.value))
        elif isinstance(node, (ast.AnnAssign, ast.NamedExpr)) and node.value is not None:
            assignments.extend(_assignment_alias_pairs(node.target, node.value))

    changed = True
    while changed:
        changed = False
        for name, value in assignments:
            kind = _environment_expression_kind(
                value,
                os_aliases=os_aliases,
                environ_aliases=environ_aliases,
                getenv_aliases=getenv_aliases,
            )
            target_set = {"os": os_aliases, "environ": environ_aliases, "getenv": getenv_aliases}.get(kind)
            if target_set is not None and name not in target_set:
                target_set.add(name)
                changed = True
    return os_aliases, environ_aliases, getenv_aliases


def _raw_credential_call(node, *, os_aliases, environ_aliases, getenv_aliases):
    candidate = None
    if isinstance(node, ast.Call) and node.args:
        if (
            _environment_expression_kind(
                node.func,
                os_aliases=os_aliases,
                environ_aliases=environ_aliases,
                getenv_aliases=getenv_aliases,
            )
            == "getenv"
        ):
            candidate = node.args[0]
    elif isinstance(node, ast.Subscript):
        if (
            _environment_expression_kind(
                node.value,
                os_aliases=os_aliases,
                environ_aliases=environ_aliases,
                getenv_aliases=getenv_aliases,
            )
            == "environ"
        ):
            candidate = node.slice
    if isinstance(candidate, ast.Constant) and isinstance(candidate.value, str):
        if _RETAINED_AUTHORITY_ENVIRONMENT_NAME.fullmatch(candidate.value):
            return candidate.value
    return None


def _raw_retained_authorities(backend_root):
    raw_retained = []
    for relative_root in ("database", "ella", "routers", "utils/ella"):
        for source_path in (backend_root / relative_root).rglob("*.py"):
            if source_path.name == "honcho_attestation.py":
                continue
            source_tree = ast.parse(source_path.read_text(encoding="utf-8"))
            os_aliases, environ_aliases, getenv_aliases = _environment_aliases(source_tree)
            parents = {child: parent for parent in ast.walk(source_tree) for child in ast.iter_child_nodes(parent)}
            for node in ast.walk(source_tree):
                environment_name = _raw_credential_call(
                    node,
                    os_aliases=os_aliases,
                    environ_aliases=environ_aliases,
                    getenv_aliases=getenv_aliases,
                )
                if not environment_name:
                    continue
                ancestors = []
                current = node
                while current in parents:
                    current = parents[current]
                    ancestors.append(current)
                    if isinstance(current, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Module)):
                        break
                scope = ancestors[-1]
                statement = next(
                    (item for item in ancestors if isinstance(item, (ast.Assign, ast.AnnAssign, ast.Return))),
                    None,
                )
                if isinstance(scope, ast.Module):
                    value = statement.value if isinstance(statement, (ast.Assign, ast.AnnAssign)) else None
                    if not isinstance(value, ast.Compare):
                        raw_retained.append((source_path.relative_to(backend_root), node.lineno, environment_name))
                    continue
                if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                global_names = {name for item in scope.body if isinstance(item, ast.Global) for name in item.names}
                assigned_targets = []
                if isinstance(statement, ast.Assign):
                    assigned_targets = statement.targets
                elif isinstance(statement, ast.AnnAssign):
                    assigned_targets = [statement.target]
                assigns_retained_attribute = any(isinstance(target, ast.Attribute) for target in assigned_targets)
                assigns_global = any(
                    isinstance(target, ast.Name) and target.id in global_names for target in assigned_targets
                )
                persistent_factory = any(
                    isinstance(item, ast.Call)
                    and (
                        (
                            isinstance(item.func, ast.Attribute)
                            and item.func.attr in {"create_pool", "Redis", "Pinecone"}
                        )
                        or (isinstance(item.func, ast.Name) and item.func.id in {"Redis", "Pinecone"})
                    )
                    for item in ancestors
                )
                retained_config_factory = scope.name == "from_env"
                if assigns_retained_attribute or assigns_global or persistent_factory or retained_config_factory:
                    raw_retained.append((source_path.relative_to(backend_root), node.lineno, environment_name))
    return raw_retained


def _assert_no_raw_retained_authorities(backend_root):
    raw_retained = _raw_retained_authorities(backend_root)
    locations = ", ".join(f"{path}:{line}" for path, line, _name in raw_retained)
    assert raw_retained == [], f"raw retained authority at {locations}"


def test_retained_authority_inventory_mutation_sensitivity_across_all_roots_and_aliases(tmp_path):
    backend_root = tmp_path / "backend"
    fixtures = {
        Path("routers/announcements.py"): "import os\n" + "\n" * 23 + 'ADMIN_KEY = os.getenv("ADMIN_KEY", "")\n',
        Path("ella/routers/chat.py"): "import os\n" + "\n" * 68 + 'XAI_API_KEY = os.getenv("XAI_API_KEY", "")\n',
        Path("database/dynamic_alias.py"): (
            "import os as system\n"
            "environment = system.environ\n"
            "read_environment = environment.get\n"
            'DATABASE_PASSWORD = read_environment("DATABASE_PASSWORD", "")\n'
        ),
        Path("utils/ella/imported_alias.py"): (
            "from os import environ as imported_environment, getenv as imported_getenv\n"
            "environment = imported_environment\n"
            "read_environment = imported_getenv\n"
            'API_SERVER_KEY = read_environment("API_SERVER_KEY", "")\n'
            'PROVISION_API_TOKEN = environment["PROVISION_API_TOKEN"]\n'
        ),
        Path("routers/getattr_alias.py"): (
            "import os\n"
            'read_environment = getattr(os, "getenv")\n'
            'WORKFLOW_API_KEY = read_environment("WORKFLOW_API_KEY", "")\n'
        ),
    }
    for relative_path, source in fixtures.items():
        source_path = backend_root / relative_path
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_text(source, encoding="utf-8")

    findings = {(path.as_posix(), line) for path, line, _name in _raw_retained_authorities(backend_root)}
    assert findings == {
        ("database/dynamic_alias.py", 4),
        ("ella/routers/chat.py", 70),
        ("routers/announcements.py", 25),
        ("routers/getattr_alias.py", 3),
        ("utils/ella/imported_alias.py", 4),
        ("utils/ella/imported_alias.py", 5),
    }
    with pytest.raises(AssertionError) as failure:
        _assert_no_raw_retained_authorities(backend_root)
    assert "routers/announcements.py:25" in str(failure.value)
    assert "ella/routers/chat.py:70" in str(failure.value)


def test_primary_fallback_authorities_preserve_outbound_selection_and_legacy_truthiness():
    backend_root = Path(__file__).resolve().parents[2]
    probe = """
import asyncio
import importlib
import json
import os
import sys
import types
from unittest.mock import MagicMock

module_name, callsite, expected = sys.argv[1:]
fallback = "synthetic-fallback-authority-value"

fake_stripe = types.ModuleType("stripe")
fake_stripe.Subscription = types.SimpleNamespace()
sys.modules["stripe"] = fake_stripe
fake_redis = types.ModuleType("redis")
fake_redis.Redis = lambda **kwargs: object()
sys.modules["redis"] = fake_redis
fake_conversations = types.ModuleType("database.conversations")
fake_conversations._decrypt_conversation_data = lambda value: value
sys.modules["database.conversations"] = fake_conversations
if callsite == "callbacks-provision":
    for dependency in (
        "database._client",
        "database.memories",
        "database.users",
        "database.ella_contacts",
        "utils.notifications",
        "utils.other.endpoints",
        "utils.other.storage",
        "ella.config",
    ):
        sys.modules[dependency] = MagicMock()

module = importlib.import_module(module_name)
captured = {}

class Response:
    status_code = 200
    text = ""

    def json(self):
        if callsite == "callbacks-provision":
            return {"internal_assessment": {"risk_level": "none"}}
        return {"choices": [{"message": {"content": "ok"}}], "answer": "ok", "confidence": "high", "sources": []}

    def raise_for_status(self):
        return None

class Client:
    def __init__(self, *args, **kwargs):
        captured["constructed"] = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, *, headers, json):
        captured["headers"] = headers
        return Response()

    async def get(self, url, *, headers):
        captured["headers"] = headers
        return Response()

module.httpx.AsyncClient = Client

if callsite == "chat-hermes":
    assert module.HERMES_GATEWAY_TOKEN == expected, "authority selection mismatch"
    asyncio.run(module._hermes_nonstream_completion([], "synthetic-session"))
    assert captured.get("headers", {}).get("Authorization") == f"Bearer {expected}", "outbound header mismatch"
elif callsite == "voice-memory":
    assert module.HERMES_VOICE_MEMORY_TOKEN == expected, "authority selection mismatch"
    asyncio.run(module._search_voice_memory_pack("synthetic-user", "hello", 1))
    if expected:
        assert captured.get("headers", {}).get("Authorization") == f"Bearer {expected}", "outbound header mismatch"
    else:
        assert captured == {}, "empty authority reached outbound transport"
elif callsite == "callbacks-provision":
    assert module.PROVISION_API_KEY == expected, "authority selection mismatch"

    async def authority_enabled(_uid=None):
        return False

    async def resolve_agent(_uid):
        return "synthetic-agent"

    module.runtime_authority_enabled = authority_enabled
    module._resolve_agent_id_for_uid = resolve_agent
    asyncio.run(module._fetch_internal_assessment("synthetic-user", "synthetic-conversation"))
    expected_headers = {"Authorization": f"Bearer {expected}"} if expected else {}
    assert captured.get("headers") == expected_headers, "outbound header mismatch"
elif callsite == "resolve-provision":
    assert module.PROVISION_API_KEY == expected, "authority selection mismatch"

    async def get_pool():
        return object()

    async def no_runtime(*args, **kwargs):
        return None

    async def owned_routing(_uid):
        return {"routing": {"agentId": "synthetic-agent"}}

    module._get_pool = get_pool
    module.EllaProvisioningRepository = lambda _pool: object()
    module.resolve_isolated_runtime = no_runtime
    module.resolve_user_routing = owned_routing
    asyncio.run(module.proxy_chat_history("synthetic-agent", authenticated_uid="synthetic-user"))
    assert captured.get("headers") == {"x-api-key": expected}, "outbound header mismatch"
elif callsite == "resolve-hermes":
    assert module.HERMES_GATEWAY_TOKEN == expected, "authority selection mismatch"

    class Pool:
        async def fetchrow(self, *args):
            return {
                "id": "11111111-1111-1111-1111-111111111111",
                "name": "Synthetic",
                "omi_uid": "synthetic-user",
                "status": "active",
                "guardian_mode": False,
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
                "agents": {"workspace": "synthetic", "userAgentId": "synthetic-agent"},
                "cluster_status": "active",
            }

    async def get_pool():
        return Pool()

    async def no_runtime(*args, **kwargs):
        return None

    module._get_pool = get_pool
    module.resolve_isolated_runtime = no_runtime
    module.retained_owner_uid_configured = lambda _uid: True
    routing = asyncio.run(module.resolve_user_routing("synthetic-user"))["routing"]
    if expected:
        assert routing.get("platform") == "hermes", "Hermes routing not selected"
        assert routing.get("token") == expected, "routing authority mismatch"
    else:
        assert routing.get("platform") != "hermes", "empty authority enabled Hermes routing"
        assert fallback not in json.dumps(routing), "fallback authority escaped empty suppression"
elif callsite == "correction-honcho":
    assert module.HONCHO_API_KEY == expected, "authority selection mismatch"
    expected_headers = {"Content-Type": "application/json"}
    if expected:
        expected_headers["Authorization"] = f"Bearer {expected}"
    assert module._honcho_headers(module.HONCHO_API_KEY) == expected_headers, "outbound header mismatch"
elif callsite == "scanner-provision":
    async def authority_enabled(_uid=None):
        return False

    module.runtime_authority_enabled = authority_enabled
    asyncio.run(module._fetch_scanner_tuning("synthetic-agent", "synthetic-user"))
    expected_headers = {"Authorization": f"Bearer {expected}"} if expected else {}
    assert captured.get("headers") == expected_headers, "outbound header mismatch"
else:
    raise AssertionError("unknown provider-free authority probe")
"""
    presence_callsites = (
        ("ella.routers.chat", "chat-hermes", "HERMES_API_SERVER_KEY", ("API_SERVER_KEY",)),
        ("ella.routers.voice", "voice-memory", "HERMES_VOICE_MEMORY_TOKEN", ("ELLA_PROVISION_API_TOKEN",)),
        ("ella.routers.callbacks", "callbacks-provision", "ELLA_PROVISION_API_KEY", ("ELLA_PROVISION_API_TOKEN",)),
        ("ella.routers.resolve", "resolve-provision", "ELLA_PROVISION_API_KEY", ("ELLA_PROVISION_API_TOKEN",)),
        ("ella.routers.resolve", "resolve-hermes", "HERMES_API_SERVER_KEY", ("API_SERVER_KEY",)),
    )
    truthy_callsites = (
        (
            "ella.services.correction_honcho_contract",
            "correction-honcho",
            "ELLA_CORRECTION_HONCHO_API_KEY",
            ("HONCHO_API_KEY",),
        ),
        (
            "utils.ella.scanner_keyterms",
            "scanner-provision",
            "ELLA_PROVISION_API_TOKEN",
            ("ELLA_PROVISION_API_KEY", "PROVISION_API_TOKEN"),
        ),
    )

    for module_name, callsite, primary_name, fallback_names in presence_callsites + truthy_callsites:
        uses_truthiness = (module_name, callsite, primary_name, fallback_names) in truthy_callsites
        scenarios = [
            (
                "explicit-empty",
                "",
                "synthetic-fallback-authority-value" if uses_truthiness else "",
                "synthetic-fallback-authority-value",
            ),
            ("absent-primary", None, "synthetic-fallback-authority-value", "synthetic-fallback-authority-value"),
            (
                "nonempty-primary",
                "synthetic-primary-authority-value",
                "synthetic-primary-authority-value",
                "synthetic-fallback-authority-value",
            ),
            (
                "surrounding-whitespace",
                "  synthetic-primary-authority-value  ",
                "  synthetic-primary-authority-value  ",
                "synthetic-fallback-authority-value",
            ),
            ("whitespace-only", "   ", "   ", "synthetic-fallback-authority-value"),
        ]
        if len(fallback_names) > 1:
            scenarios.append(("empty-secondary", None, "synthetic-tertiary-authority-value", ""))
        for scenario, primary_value, expected, first_fallback_value in scenarios:
            environment = {
                name: value
                for name, value in os.environ.items()
                if not _RETAINED_AUTHORITY_ENVIRONMENT_NAME.fullmatch(name)
                and name != "ELLA_HERMES_PROVISION_ATTESTATION_KEY"
            }
            environment.update(
                {
                    "PYTHONPATH": str(backend_root),
                    "ENCRYPTION_SECRET": "synthetic-distinct-encryption-authority-000000000001",
                    "FIRESTORE_EMULATOR_HOST": "127.0.0.1:8080",
                    "GOOGLE_CLOUD_PROJECT": "synthetic-local",
                    "HERMES_GATEWAY_URL": "http://synthetic.invalid",
                    "HERMES_GATEWAY_PUBLIC_URL": "http://synthetic.invalid",
                    "HERMES_VOICE_MEMORY_URL": "http://synthetic.invalid",
                    "HERMES_MODEL": "synthetic-model",
                    "ELLA_CHAT_PLATFORM": "hermes",
                }
            )
            if primary_value is not None:
                environment[primary_name] = primary_value
            for index, fallback_name in enumerate(fallback_names):
                environment[fallback_name] = (
                    first_fallback_value if index == 0 else "synthetic-tertiary-authority-value"
                )
            completed = subprocess.run(
                [sys.executable, "-c", probe, module_name, callsite, expected],
                cwd=backend_root,
                env=environment,
                capture_output=True,
                text=True,
                check=False,
            )
            assert completed.returncode == 0, f"provider-free authority probe failed for {callsite}:{scenario}"
            assert completed.stdout == ""
            assert completed.stderr == ""


def test_authority_credential_presence_optional_none_and_redacted_separation_contract():
    backend_root = Path(__file__).resolve().parents[2]
    probe = """
import os
import sys

mode = sys.argv[1]
primary = "SYNTHETIC_PRIMARY_API_KEY"
fallback = "SYNTHETIC_FALLBACK_API_KEY"
selected = "synthetic-selected-authority-value"
unselected = "synthetic-unselected-authority-value"

from database.honcho_attestation import HonchoAttestationError, authority_credential, create_challenge

def challenge():
    return create_challenge(
        firebase_uid="synthetic-user",
        account_owner_id="synthetic-owner",
        runtime_target_id="synthetic-target",
        binding_id="synthetic-binding",
        job_id="synthetic-job",
    )

if mode == "empty-suppresses":
    os.environ[primary] = ""
    os.environ[fallback] = unselected
    assert authority_credential(primary, fallback) == "", "presence contract mismatch"
    os.environ[fallback] = "synthetic-current-authority-value"
    os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = unselected
    challenge()
elif mode == "absent-fallback":
    os.environ.pop(primary, None)
    os.environ[fallback] = selected
    assert authority_credential(primary, fallback) == selected, "presence contract mismatch"
    os.environ[fallback] = "synthetic-current-authority-value"
    os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = selected
    try:
        challenge()
    except HonchoAttestationError as exc:
        assert str(exc) == "honcho_attestation_key_conflict", "non-fixed separation error"
    else:
        raise AssertionError("selected authority was not retained")
elif mode == "whitespace":
    os.environ[primary] = "   "
    os.environ[fallback] = unselected
    assert authority_credential(primary, fallback) == "", "stripped whitespace contract mismatch"
    assert authority_credential(primary, fallback, strip=False) == "   ", "raw whitespace contract mismatch"
elif mode == "optional-none":
    os.environ.pop(primary, None)
    assert authority_credential(primary, default=None) is None, "optional default contract mismatch"
elif mode == "redacted-error":
    secret_name = "synthetic-lowercase-secret-name"
    secret_value = "synthetic-secret-value-that-must-not-leak"
    try:
        authority_credential(secret_name, default=secret_value)
    except ValueError as exc:
        assert str(exc) == "invalid_authority_credential_reference", "non-fixed validation error"
        assert secret_name not in str(exc) and secret_value not in str(exc), "authority detail leaked"
    else:
        raise AssertionError("invalid authority name accepted")
else:
    raise AssertionError("unknown authority contract probe")
"""
    for mode in ("empty-suppresses", "absent-fallback", "whitespace", "optional-none", "redacted-error"):
        environment = {
            name: value
            for name, value in os.environ.items()
            if not _RETAINED_AUTHORITY_ENVIRONMENT_NAME.fullmatch(name)
            and name != "ELLA_HERMES_PROVISION_ATTESTATION_KEY"
        }
        environment["PYTHONPATH"] = str(backend_root)
        completed = subprocess.run(
            [sys.executable, "-c", probe, mode],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"authority contract probe failed for {mode}"
        assert completed.stdout == ""
        assert completed.stderr == ""


def test_attestation_key_separation_retains_real_honcho_consumer_authorities_after_env_reload():
    backend_root = Path(__file__).resolve().parents[2]
    credential_name = _RETAINED_AUTHORITY_ENVIRONMENT_NAME
    _assert_no_raw_retained_authorities(backend_root)

    probe = """
import asyncio
import importlib
import json
import os
import sys
import types

module_name, environment_name, mode = sys.argv[1:]
retained = "synthetic-retained-honcho-authority-value-A"
replacement = "synthetic-current-honcho-authority-value-B"
attestation = retained.lower() if mode == "photon-comma" else retained
initial = f" {retained} " if mode == "chat-xai-whitespace" else retained
if mode == "photon-comma":
    initial = f"{'a' * 64},{retained},{'b' * 64}"
if mode in {"guardian-tts", "guardian-service"}:
    os.environ.setdefault("GUARDIAN_WEBHOOK_KEY", "synthetic-distinct-guardian-service-key-000001")
if mode == "guardian-xai":
    os.environ.pop("OPENROUTER_API_KEY", None)
os.environ[environment_name] = initial

captured = {}
if mode.startswith("voice-"):
    fake_stripe = types.ModuleType("stripe")
    fake_stripe.Subscription = types.SimpleNamespace()
    sys.modules["stripe"] = fake_stripe
    fake_redis = types.ModuleType("redis")
    fake_redis.Redis = lambda **kwargs: object()
    sys.modules["redis"] = fake_redis
    fake_conversations = types.ModuleType("database.conversations")
    fake_conversations._decrypt_conversation_data = lambda value: value
    sys.modules["database.conversations"] = fake_conversations
elif mode == "redis":
    fake_redis = types.ModuleType("redis")
    fake_redis.Redis = lambda **kwargs: captured.update(kwargs) or object()
    sys.modules["redis"] = fake_redis
elif mode == "pinecone":
    class SyntheticPinecone:
        def __init__(self, *, api_key):
            captured["api_key"] = api_key

        def Index(self, name):
            return object()

    fake_pinecone = types.ModuleType("pinecone")
    fake_pinecone.Pinecone = SyntheticPinecone
    sys.modules["pinecone"] = fake_pinecone
    fake_models = types.ModuleType("models")
    fake_conversation = types.ModuleType("models.conversation")
    fake_conversation.Conversation = object
    fake_models.conversation = fake_conversation
    sys.modules["models"] = fake_models
    sys.modules["models.conversation"] = fake_conversation
    fake_llm = types.ModuleType("utils.llm.clients")
    fake_llm.embeddings = object()
    sys.modules["utils.llm.clients"] = fake_llm

module = importlib.import_module(module_name)
retained_object = None
if mode == "policy-signing":
    retained_object = module.ApprovedRuntimeManifestStore(path="/synthetic", signing_key=None)
elif mode in {"photon-identity", "photon-comma"}:
    retained_object = module.PhotonAdapterConfig.from_env()
os.environ[environment_name] = replacement

if mode == "correction":
    would_send_retained = module._honcho_headers(module.HONCHO_API_KEY).get("Authorization") == f"Bearer {retained}"
elif mode == "voice-honcho":
    would_send_retained = module._headers().get("Authorization") == f"Bearer {retained}"
elif mode == "profile-map":
    class SyntheticResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b"{}"

    def synthetic_urlopen(request, *, timeout):
        captured["authorization"] = request.get_header("Authorization")
        return SyntheticResponse()

    module.urlopen = synthetic_urlopen
    module._safe_json_url("https://synthetic.invalid/profile-map")
    would_send_retained = captured.get("authorization") == f"Bearer {retained}"
elif mode in {"auto-provision", "auto-gateway"}:
    class SyntheticPool:
        async def fetchrow(self, *args):
            return {
                "id": "00000000-0000-0000-0000-000000000001",
                "cluster_id": None,
                "name": "Synthetic",
                "email": None,
                "timezone": "UTC",
                "conditions": [],
                "medications": [],
                "identities": {},
            }

        async def execute(self, query, *args):
            captured["cluster"] = json.loads(args[1])

    async def synthetic_pool():
        return SyntheticPool()

    async def disabled(uid):
        return False

    class SyntheticProvisionResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"provisionedAt": "2026-01-01T00:00:00Z"}

    class SyntheticAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["authorization"] = kwargs["headers"].get("Authorization")
            return SyntheticProvisionResponse()

    module._get_pool = synthetic_pool
    module.runtime_resolver.runtime_authority_enabled = disabled
    module.httpx.AsyncClient = SyntheticAsyncClient
    result = asyncio.run(module.auto_provision_user("synthetic-user"))
    assert result["success"] is True
    would_send_retained = (
        captured.get("authorization") == f"Bearer {retained}"
        if mode == "auto-provision"
        else captured.get("cluster", {}).get("gatewayToken") == retained
    )
elif mode in {"chat-xai", "chat-xai-whitespace"}:
    class SyntheticStreamResponse:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def aiter_lines(self):
            if False:
                yield ""

    class SyntheticAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def stream(self, *args, **kwargs):
            captured["authorization"] = kwargs["headers"]["Authorization"]
            return SyntheticStreamResponse()

    module.httpx.AsyncClient = SyntheticAsyncClient

    async def consume():
        async for _ in module._stream_level_2_grok("hello"):
            pass

    asyncio.run(consume())
    would_send_retained = captured.get("authorization") == f"Bearer {initial}"
elif mode == "guardian-service":
    module._verify_key(initial, None, "synthetic-user")
    would_send_retained = True
elif mode == "guardian-provision":
    async def no_timeline(*args, **kwargs):
        return []

    async def disabled(uid):
        return False

    async def resolved(uid):
        return {"routing": {"agentId": "ella-synthetic"}}

    class SyntheticResponse:
        status_code = 200

        def json(self):
            return {"messages": []}

    class SyntheticAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, *args, **kwargs):
            captured["authorization"] = kwargs["headers"].get("Authorization")
            return SyntheticResponse()

    module.fetch_canonical_timeline = no_timeline
    module.runtime_authority_enabled = disabled
    module.resolve_user_routing = resolved
    module.httpx.AsyncClient = SyntheticAsyncClient
    asyncio.run(module._get_recent_chat_turns("synthetic-user"))
    would_send_retained = captured.get("authorization") == f"Bearer {retained}"
elif mode in {"guardian-openrouter", "guardian-xai"}:
    async def disabled(uid):
        return False

    class SyntheticResponse:
        status_code = 200

        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}

    class SyntheticAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["authorization"] = kwargs["headers"]["Authorization"]
            return SyntheticResponse()

    module.assert_current_ai_consent = lambda uid: None
    module.runtime_authority_enabled = disabled
    module.httpx.AsyncClient = SyntheticAsyncClient
    asyncio.run(module._consolidate_queue("synthetic-user", [{"message": "one"}], [], []))
    would_send_retained = captured.get("authorization") == f"Bearer {retained}"
elif mode == "guardian-tts":
    class SyntheticResponse:
        status_code = 200
        content = b"audio"
        headers = {"content-type": "audio/mpeg"}

    class SyntheticAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["token"] = kwargs["headers"].get("X-Ella-Internal-Token")
            return SyntheticResponse()

    module.app_settings_db.get_voice_settings = lambda uid: {}
    module.build_effective_voice_settings = lambda uid, settings: {
        "effective_voice_settings": {"voice_mode": "v3-rich", "fallback_used": False}
    }
    module._guardian_tts_candidates = lambda effective, requested: ["elevenlabs"]
    module.httpx.AsyncClient = SyntheticAsyncClient
    request = module.SynthesizeRequest(uid="synthetic-user", text="hello")
    asyncio.run(
        module.synthesize_audio(
            request,
            "synthetic-distinct-guardian-service-key-000001",
            None,
            "synthetic-user",
        )
    )
    would_send_retained = captured.get("token") == retained
elif mode in {"voice-xai", "voice-inworld", "voice-elevenlabs"}:
    class SyntheticResponse:
        status_code = 200
        content = b"audio"
        headers = {"content-type": "audio/mpeg"}
        text = '{"result":{"audioContent":"YXVkaW8="}}'

    class SyntheticAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            captured["headers"] = kwargs["headers"]
            return SyntheticResponse()

    module.httpx.AsyncClient = SyntheticAsyncClient
    provider = {"voice-xai": "xai-tts", "voice-inworld": "inworld", "voice-elevenlabs": "elevenlabs"}[mode]
    asyncio.run(module.synthesize_speech(module.TtsRequest(text="hello"), provider))
    headers = captured["headers"]
    actual = headers.get("Authorization") if mode != "voice-elevenlabs" else headers.get("xi-api-key")
    expected = f"Bearer {retained}" if mode == "voice-xai" else f"Basic {retained}"
    if mode == "voice-elevenlabs":
        expected = retained
    would_send_retained = actual == expected
elif mode == "escalation":
    would_send_retained = module._has_valid_service_key(None, retained, None)
elif mode == "scanner":
    module.requests.post = lambda *args, **kwargs: captured.update(kwargs["headers"])
    module._log_trace_event("trace", "user", "stage", "ok")
    would_send_retained = captured.get("X-Guardian-Key") == retained
elif mode == "scanner-db":
    class SyntheticCursor:
        rowcount = 0

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, *args):
            pass

        def fetchone(self):
            return ("off",)

    class SyntheticConnection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def cursor(self):
            return SyntheticCursor()

        def close(self):
            pass

    fake_psycopg2 = types.ModuleType("psycopg2")
    fake_psycopg2.connect = lambda **kwargs: captured.update(kwargs) or SyntheticConnection()
    sys.modules["psycopg2"] = fake_psycopg2
    module._insert_wake_ack_direct(
        "synthetic-user",
        "synthetic-trace",
        {"id": "item", "url": "https://synthetic.invalid/audio", "metadata": {}},
    )
    would_send_retained = captured.get("password") == retained
elif mode == "redis":
    would_send_retained = captured.get("password") == retained
elif mode == "pinecone":
    would_send_retained = captured.get("api_key") == retained
elif mode == "policy-signing":
    would_send_retained = retained_object.signing_key == retained
elif mode == "photon-identity":
    expected = __import__("hmac").new(
        retained.encode(), b"scope\x1fidentity", __import__("hashlib").sha256
    ).hexdigest()
    would_send_retained = retained_object.opaque_key("scope", "identity") == expected
elif mode == "photon-comma":
    would_send_retained = retained.lower() in retained_object.synthetic_message_keys
else:
    raise AssertionError("unknown retained-authority probe")
assert would_send_retained

os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = attestation
from database.honcho_attestation import HonchoAttestationError, create_challenge

try:
    create_challenge(
        firebase_uid="synthetic-user",
        account_owner_id="synthetic-owner",
        runtime_target_id="synthetic-target",
        binding_id="synthetic-binding",
        job_id="synthetic-job",
    )
except HonchoAttestationError as exc:
    assert exc.code == "honcho_attestation_key_conflict"
else:
    raise AssertionError("retained authority was hidden by environment reload")
"""
    consumers = (
        ("ella.services.correction_honcho_contract", "ELLA_CORRECTION_HONCHO_API_KEY", "correction"),
        ("ella.services.correction_honcho_contract", "HONCHO_API_KEY", "correction"),
        ("ella.services.voice_honcho", "ELLA_VOICE_HONCHO_API_KEY", "voice-honcho"),
        ("ella.services.voice_honcho", "HONCHO_API_KEY", "voice-honcho"),
        ("ella.services.correction_honcho_contract", "ELLA_CORRECTION_HONCHO_PROFILE_MAP_TOKEN", "profile-map"),
        ("ella.routers.auto_provision", "ELLA_PROVISION_API_TOKEN", "auto-provision"),
        ("ella.routers.auto_provision", "OPENCLAW_GATEWAY_TOKEN", "auto-gateway"),
        ("ella.routers.chat", "XAI_API_KEY", "chat-xai"),
        ("ella.routers.chat", "XAI_API_KEY", "chat-xai-whitespace"),
        ("ella.routers.guardian", "GUARDIAN_WEBHOOK_KEY", "guardian-service"),
        ("ella.routers.guardian", "ELLA_INTERNAL_VOICE_TTS_TOKEN", "guardian-tts"),
        ("ella.routers.guardian", "ELLA_PROVISION_API_TOKEN", "guardian-provision"),
        ("ella.routers.guardian", "OPENROUTER_API_KEY", "guardian-openrouter"),
        ("ella.routers.guardian", "XAI_API_KEY", "guardian-xai"),
        ("ella.routers.voice", "XAI_API_KEY", "voice-xai"),
        ("ella.routers.voice", "INWORLD_API_KEY", "voice-inworld"),
        ("ella.routers.voice", "ELEVENLABS_API_KEY", "voice-elevenlabs"),
        ("ella.routers.escalations", "ELLA_ESCALATION_WEBHOOK_KEY", "escalation"),
        ("ella.routers.escalations", "GUARDIAN_WEBHOOK_KEY", "escalation"),
        ("utils.ella.scanner", "GUARDIAN_WEBHOOK_KEY", "scanner"),
        ("utils.ella.scanner", "ELLA_POSTGRES_PASSWORD", "scanner-db"),
        ("database.redis_db", "REDIS_DB_PASSWORD", "redis"),
        ("database.vector_db", "PINECONE_API_KEY", "pinecone"),
        ("ella.services.hermes_cloud_policy", "ELLA_HERMES_CLOUD_APPROVAL_SIGNING_KEY", "policy-signing"),
        ("ella.services.hermes_cloud_photon", "ELLA_HERMES_CLOUD_PHOTON_IDENTITY_HMAC_KEY", "photon-identity"),
        ("ella.services.hermes_cloud_photon", "ELLA_HERMES_CLOUD_PHOTON_SYNTHETIC_MESSAGE_KEYS", "photon-comma"),
    )
    for module_name, environment_name, mode in consumers:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not credential_name.fullmatch(name) and name != "ELLA_HERMES_PROVISION_ATTESTATION_KEY"
        }
        environment["PYTHONPATH"] = str(backend_root)
        environment["ENCRYPTION_SECRET"] = "synthetic-distinct-encryption-authority-000000000001"
        environment.setdefault("FIRESTORE_EMULATOR_HOST", "127.0.0.1:8080")
        environment.setdefault("GOOGLE_CLOUD_PROJECT", "synthetic-local")
        completed = subprocess.run(
            [sys.executable, "-c", probe, module_name, environment_name, mode],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"retained authority protection failed for {module_name}:{mode}"

    explicit_empty_probe = """
import importlib
import os
import sys

module_name, primary_name, fallback_name = sys.argv[1:]
retained = "synthetic-unselected-fallback-authority-value-A"
os.environ[primary_name] = ""
os.environ[fallback_name] = retained
module = importlib.import_module(module_name)
configured = module.HONCHO_API_KEY if module_name.endswith("voice_honcho") else module.ESCALATION_WEBHOOK_KEY
assert configured == ""
if module_name.endswith("voice_honcho"):
    raise SystemExit(0)
os.environ[fallback_name] = "synthetic-current-fallback-authority-value-B"
os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = retained
from database.honcho_attestation import create_challenge

create_challenge(
    firebase_uid="synthetic-user",
    account_owner_id="synthetic-owner",
    runtime_target_id="synthetic-target",
    binding_id="synthetic-binding",
    job_id="synthetic-job",
)
"""
    for module_name, primary_name, fallback_name in (
        ("ella.services.voice_honcho", "ELLA_VOICE_HONCHO_API_KEY", "HONCHO_API_KEY"),
        ("ella.routers.escalations", "ELLA_ESCALATION_WEBHOOK_KEY", "GUARDIAN_WEBHOOK_KEY"),
    ):
        environment = {
            name: value
            for name, value in os.environ.items()
            if not credential_name.fullmatch(name) and name != "ELLA_HERMES_PROVISION_ATTESTATION_KEY"
        }
        environment["PYTHONPATH"] = str(backend_root)
        environment["ENCRYPTION_SECRET"] = "synthetic-distinct-encryption-authority-000000000001"
        environment["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:8080"
        environment["GOOGLE_CLOUD_PROJECT"] = "synthetic-local"
        completed = subprocess.run(
            [sys.executable, "-c", explicit_empty_probe, module_name, primary_name, fallback_name],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"primary presence semantics changed for {module_name}"

    pool_probe = """
import asyncio
import importlib
import os
import sys
import types

module_name, getter_name, mode = sys.argv[1:]
retained = "synthetic-retained-postgres-authority-value-A"
replacement = "synthetic-current-postgres-authority-value-B"
os.environ.pop("ELLA_POSTGRES_DSN", None)
if mode == "dsn":
    os.environ["ELLA_POSTGRES_DSN"] = retained
else:
    os.environ["ELLA_POSTGRES_PASSWORD"] = retained

if module_name in {"ella.routers.callbacks", "ella.routers.voice"}:
    fake_stripe = types.ModuleType("stripe")
    fake_stripe.Subscription = types.SimpleNamespace()
    sys.modules["stripe"] = fake_stripe
    fake_redis = types.ModuleType("redis")
    fake_redis.Redis = lambda **kwargs: object()
    sys.modules["redis"] = fake_redis
if module_name == "ella.routers.callbacks":
    for dependency in ("database.conversations", "database.memories", "database.users"):
        sys.modules[dependency] = types.ModuleType(dependency)
    fake_database_client = types.ModuleType("database._client")
    fake_database_client.db = object()
    sys.modules["database._client"] = fake_database_client
    fake_conversation_model = types.ModuleType("models.conversation")
    fake_conversation_model.CategoryEnum = str
    sys.modules["models.conversation"] = fake_conversation_model
    fake_contacts = types.ModuleType("database.ella_contacts")
    for name in ("create_contact", "delete_contact", "get_contact", "get_contacts", "update_contact"):
        setattr(fake_contacts, name, lambda *args, **kwargs: None)
    sys.modules["database.ella_contacts"] = fake_contacts
    fake_config = types.ModuleType("ella.config")
    fake_config.ELLA_CONFIG = object()
    sys.modules["ella.config"] = fake_config
    fake_sanitizer = types.ModuleType("ella.services.summary_sanitizer")
    fake_sanitizer.SummarySanitizationError = type("SummarySanitizationError", (Exception,), {})
    sys.modules["ella.services.summary_sanitizer"] = fake_sanitizer
    fake_writeback = types.ModuleType("ella.services.summary_writeback")
    fake_writeback.CanonicalConversationSourceMismatchError = type(
        "CanonicalConversationSourceMismatchError", (Exception,), {}
    )
    fake_writeback.CanonicalSummaryDependencyUnavailableError = type(
        "CanonicalSummaryDependencyUnavailableError", (Exception,), {}
    )
    fake_writeback.CanonicalSummaryOperationConflictError = type(
        "CanonicalSummaryOperationConflictError", (Exception,), {}
    )
    fake_writeback.CanonicalSummaryReconciliationPendingError = type(
        "CanonicalSummaryReconciliationPendingError", (Exception,), {}
    )
    fake_writeback.ConversationSummaryNotFoundError = type("ConversationSummaryNotFoundError", (Exception,), {})
    fake_writeback.ConversationSummaryOutcomeUnknownError = type(
        "ConversationSummaryOutcomeUnknownError", (Exception,), {}
    )
    fake_writeback.InvalidConversationSummaryCategoryError = type(
        "InvalidConversationSummaryCategoryError", (Exception,), {}
    )
    fake_writeback.write_conversation_summary = lambda *args, **kwargs: None
    fake_writeback.write_conversation_summary_cas = lambda *args, **kwargs: None
    sys.modules["ella.services.summary_writeback"] = fake_writeback
    fake_notifications = types.ModuleType("utils.notifications")
    fake_notifications.send_notification = lambda *args, **kwargs: None
    sys.modules["utils.notifications"] = fake_notifications
    fake_canonical_omi = types.ModuleType("utils.ella.canonical_omi")
    fake_canonical_omi.require_omi_canonical_write_ready = lambda *args, **kwargs: None
    fake_canonical_omi.write_omi_canonical_event = lambda *args, **kwargs: None
    sys.modules["utils.ella.canonical_omi"] = fake_canonical_omi
    fake_exact_auth = types.ModuleType("utils.ella.exact_firebase_auth")
    fake_exact_auth.ELLA_SUBJECT_UID_HEADER = "X-Ella-Subject-Uid"
    fake_exact_auth.EllaRequestAuthority = object
    fake_exact_auth.get_exact_firebase_uid = lambda *args, **kwargs: None
    fake_exact_auth.get_exact_service_authority = lambda *args, **kwargs: None
    fake_exact_auth.require_matching_firebase_uid = lambda *args, **kwargs: None
    sys.modules["utils.ella.exact_firebase_auth"] = fake_exact_auth
    fake_storage = types.ModuleType("utils.other.storage")
    fake_storage.storage_client = object()
    sys.modules["utils.other.storage"] = fake_storage
if module_name == "ella.routers.voice":
    fake_conversations = types.ModuleType("database.conversations")
    fake_conversations._decrypt_conversation_data = lambda value: value
    sys.modules["database.conversations"] = fake_conversations

module = importlib.import_module(module_name)
captured = {}

async def synthetic_create_pool(*args, **kwargs):
    captured.update(kwargs)
    return object()

module.asyncpg.create_pool = synthetic_create_pool
asyncio.run(getattr(module, getter_name)())
assert captured.get("dsn" if mode == "dsn" else "password") == retained
if mode == "dsn":
    os.environ["ELLA_POSTGRES_DSN"] = replacement
else:
    os.environ["ELLA_POSTGRES_PASSWORD"] = replacement
os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = retained
from database.honcho_attestation import HonchoAttestationError, create_challenge

try:
    create_challenge(
        firebase_uid="synthetic-user",
        account_owner_id="synthetic-owner",
        runtime_target_id="synthetic-target",
        binding_id="synthetic-binding",
        job_id="synthetic-job",
    )
except HonchoAttestationError as exc:
    assert exc.code == "honcho_attestation_key_conflict"
else:
    raise AssertionError("retained pool authority was hidden by environment reload")
"""
    pool_consumers = (
        ("database.ella_provisioning", "get_pool", "password"),
        ("database.voice_canary", "get_pool", "password"),
        ("database.voice_canary", "get_pool", "dsn"),
        ("ella.routers.auto_provision", "_get_pool", "password"),
        ("ella.routers.callbacks", "_get_resolve_pool", "password"),
        ("ella.routers.canonical_events", "_get_pool", "password"),
        ("ella.routers.escalations", "_get_pool", "password"),
        ("ella.routers.guardian", "_get_pool", "password"),
        ("ella.routers.resolve", "_get_pool", "password"),
        ("ella.routers.voice", "_get_pool", "password"),
        ("ella.services.observer_logs", "_get_pool", "password"),
        ("ella.utils.auto_provision", "_get_pool", "password"),
        ("utils.ella.scanner_keyterms", "_get_pool", "password"),
    )
    for module_name, getter_name, mode in pool_consumers:
        environment = {
            name: value
            for name, value in os.environ.items()
            if not credential_name.fullmatch(name)
            and name not in {"ELLA_HERMES_PROVISION_ATTESTATION_KEY", "ELLA_POSTGRES_DSN"}
        }
        environment["PYTHONPATH"] = str(backend_root)
        environment["ENCRYPTION_SECRET"] = "synthetic-distinct-encryption-authority-000000000001"
        environment["FIRESTORE_EMULATOR_HOST"] = "127.0.0.1:8080"
        environment["GOOGLE_CLOUD_PROJECT"] = "synthetic-local"
        completed = subprocess.run(
            [sys.executable, "-c", pool_probe, module_name, getter_name, mode],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"retained pool authority protection failed for {module_name}:{mode}"

    retained_value_probe = """
import importlib
import os
import sys
import types

module_name, mode = sys.argv[1:]
retained = "synthetic-retained-injected-authority-value-A"
replacement = "synthetic-current-injected-authority-value-B"
captured = {}
if mode == "identity-db":
    extras = types.ModuleType("psycopg2.extras")
    extras.RealDictCursor = object
    driver = types.ModuleType("psycopg2")
    driver.__path__ = []
    driver.extras = extras

    class SyntheticCursor:
        description = []
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def execute(self, *args): pass
        def fetchall(self): return []

    class SyntheticConnection:
        def cursor(self, **kwargs): return SyntheticCursor()
        def close(self): pass

    driver.connect = lambda **kwargs: captured.update(kwargs) or SyntheticConnection()
    sys.modules["psycopg2"] = driver
    sys.modules["psycopg2.extras"] = extras
    os.environ["ELLA_POSTGRES_PASSWORD"] = retained

module = importlib.import_module(module_name)
if mode == "identity-db":
    module._get_changed_users("2026-01-01T00:00:00Z")
    assert captured.get("password") == retained
    os.environ["ELLA_POSTGRES_PASSWORD"] = replacement
else:
    store = module.ApprovedRuntimeManifestStore(path="/synthetic", signing_key=retained)
    assert store.signing_key == retained
    os.environ["ELLA_HERMES_CLOUD_APPROVAL_SIGNING_KEY"] = replacement
os.environ["ELLA_HERMES_PROVISION_ATTESTATION_KEY"] = retained
from database.honcho_attestation import HonchoAttestationError, create_challenge

try:
    create_challenge(
        firebase_uid="synthetic-user",
        account_owner_id="synthetic-owner",
        runtime_target_id="synthetic-target",
        binding_id="synthetic-binding",
        job_id="synthetic-job",
    )
except HonchoAttestationError as exc:
    assert exc.code == "honcho_attestation_key_conflict"
else:
    raise AssertionError("retained non-environment authority was hidden")
"""
    for module_name, mode in (
        ("ella.utils.identity_sync", "identity-db"),
        ("ella.services.hermes_cloud_policy", "policy-injected"),
    ):
        environment = {
            name: value
            for name, value in os.environ.items()
            if not credential_name.fullmatch(name) and name != "ELLA_HERMES_PROVISION_ATTESTATION_KEY"
        }
        environment["PYTHONPATH"] = str(backend_root)
        completed = subprocess.run(
            [sys.executable, "-c", retained_value_probe, module_name, mode],
            cwd=backend_root,
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        assert completed.returncode == 0, f"retained injected authority protection failed for {module_name}:{mode}"


def test_attestation_window_covers_max_slow_response_and_rejects_invalid_config(monkeypatch):
    blocked_head_ttl_seconds = 120
    assert DEFAULT_PROVISION_TIMEOUT_SECONDS >= blocked_head_ttl_seconds
    assert MAX_PROVISION_TIMEOUT_SECONDS > blocked_head_ttl_seconds
    assert MAX_PROVISION_TIMEOUT_SECONDS + DEFAULT_ATTESTATION_VERIFICATION_GRACE_SECONDS < ATTESTATION_TTL_SECONDS

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monkeypatch.setenv("ELLA_HERMES_PROVISION_API_TIMEOUT_SECONDS", str(MAX_PROVISION_TIMEOUT_SECONDS))
    clock = [1_700_000_000.0]

    class SlowProvisionClient(FakeProvisionClient):
        async def provision(self, identity, target_schema_version, *, attestation_challenge):
            self.calls.append((identity, target_schema_version, attestation_challenge))
            clock[0] = attestation_challenge["issued_at"] + MAX_PROVISION_TIMEOUT_SECONDS
            return _attach_attestation(copy.deepcopy(self.result), attestation_challenge)

    repository = FakeRepository()
    client = SlowProvisionClient(_runtime_receipt())
    asyncio.run(
        ProvisioningCoordinator(repository, client, clock=lambda: clock[0]).process_claimed_job(
            job=_job(state="provisioning", stage="profile_ready"),
            identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
        )
    )
    challenge = client.calls[0][2]
    assert challenge["expires_at"] - challenge["issued_at"] == ATTESTATION_TTL_SECONDS
    assert challenge["expires_at"] - int(clock[0]) > DEFAULT_ATTESTATION_VERIFICATION_GRACE_SECONDS
    assert repository.job["state"] == "ready"
    assert repository.activation_calls == 1

    monkeypatch.setenv(ATTESTATION_VERIFICATION_GRACE_ENV, "60")
    with pytest.raises(ProvisioningError, match="honcho_attestation_window_invalid"):
        validated_provision_timeout_seconds()
    rejected_repository = FakeRepository()
    rejected_client = FakeProvisionClient(_runtime_receipt())
    asyncio.run(
        ProvisioningCoordinator(rejected_repository, rejected_client).process_claimed_job(
            job=_job(state="provisioning", stage="profile_ready"),
            identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
        )
    )
    assert rejected_client.calls == []
    assert rejected_repository.staged is None
    assert rejected_repository.activation_calls == 0
    assert rejected_repository.job["error_code"] == "honcho_attestation_window_invalid"


def test_total_deadline_cancels_real_local_slow_drip_before_binding_writes(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        provisioning_service,
        "provision_deadline",
        lambda: ProvisionDeadline(provider_timeout_seconds=1.0, verification_grace_seconds=0.01, total_seconds=0.5),
    )

    async def scenario():
        request_received = asyncio.Event()
        response_finished = asyncio.Event()

        async def slow_drip(reader, writer):
            try:
                headers = await reader.readuntil(b"\r\n\r\n")
                content_length = 0
                for line in headers.decode("ascii").split("\r\n"):
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip())
                if content_length:
                    await reader.readexactly(content_length)
                request_received.set()
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 9\r\nContent-Type: application/json\r\n\r\n")
                await writer.drain()
                for byte in b'{"a":1}\n':
                    writer.write(bytes([byte]))
                    await writer.drain()
                    await asyncio.sleep(0.1)
            except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionResetError):
                pass
            finally:
                writer.close()
                try:
                    await writer.wait_closed()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    response_finished.set()

        server = await asyncio.start_server(slow_drip, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        repository = FakeRepository()
        coordinator = ProvisioningCoordinator(repository, _local_provision_client(f"http://127.0.0.1:{port}"))
        async with server:
            await coordinator.process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
            )
            await asyncio.wait_for(request_received.wait(), timeout=2.0)
            await asyncio.wait_for(response_finished.wait(), timeout=2.0)

        assert repository.staged is None
        assert repository.activation_calls == 0
        assert repository.job["state"] == "retryable"
        assert repository.job["error_code"] == "provision_transaction_timeout"
        assert repository.job_calls[-1]["receipt"] == {
            "type": "runtime_attestation_reconciliation_required",
            "same_job_binding_required": True,
            "content_free": True,
        }

    asyncio.run(scenario())


def test_oversized_provision_receipt_is_rejected_before_parse_or_binding_write(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monkeypatch.setattr(
        provisioning_service,
        "provision_deadline",
        lambda: ProvisionDeadline(provider_timeout_seconds=0.2, verification_grace_seconds=0.1, total_seconds=0.3),
    )

    async def scenario():
        async def oversized(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(
                (
                    int(line.split(":", 1)[1].strip())
                    for line in headers.decode("ascii").split("\r\n")
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            if content_length:
                await reader.readexactly(content_length)
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Length: {MAX_PROVISION_RESPONSE_BYTES + 1}\r\n"
                    "Content-Type: application/json\r\n\r\n"
                ).encode("ascii")
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(oversized, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        repository = FakeRepository()
        coordinator = ProvisioningCoordinator(repository, _local_provision_client(f"http://127.0.0.1:{port}"))
        async with server:
            await coordinator.process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
            )

        assert repository.staged is None
        assert repository.activation_calls == 0
        assert repository.job["state"] == "retryable"
        assert repository.job["error_code"] == "invalid_provision_receipt"

    asyncio.run(scenario())


def test_total_deadline_rejects_delayed_json_parse_before_binding_write(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monotonic = [100.0]
    monkeypatch.setattr(
        provisioning_service,
        "provision_deadline",
        lambda: ProvisionDeadline(provider_timeout_seconds=0.2, verification_grace_seconds=0.8, total_seconds=1.0),
    )
    original_loads = provisioning_service.json.loads

    def delayed_loads(payload, *args, **kwargs):
        result = original_loads(payload, *args, **kwargs)
        if isinstance(payload, bytes):
            monotonic[0] += 2.0
        return result

    monkeypatch.setattr(provisioning_service.json, "loads", delayed_loads)

    async def scenario():
        async def signed_receipt(reader, writer):
            headers = await reader.readuntil(b"\r\n\r\n")
            content_length = next(
                (
                    int(line.split(":", 1)[1].strip())
                    for line in headers.decode("ascii").split("\r\n")
                    if line.lower().startswith("content-length:")
                ),
                0,
            )
            request_body = await reader.readexactly(content_length)
            request_payload = provisioning_service.json.JSONDecoder().decode(request_body.decode("utf-8"))
            response_payload = _attach_attestation(_runtime_receipt(), request_payload["honchoAttestationChallenge"])
            response_body = provisioning_service.json.dumps(response_payload).encode("utf-8")
            writer.write(
                (
                    "HTTP/1.1 200 OK\r\n"
                    f"Content-Length: {len(response_body)}\r\n"
                    "Content-Type: application/json\r\n\r\n"
                ).encode("ascii")
                + response_body
            )
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(signed_receipt, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        repository = FakeRepository()
        coordinator = ProvisioningCoordinator(
            repository,
            _local_provision_client(f"http://127.0.0.1:{port}"),
            monotonic_clock=lambda: monotonic[0],
        )
        async with server:
            await coordinator.process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
            )

        assert repository.staged is None
        assert repository.activation_calls == 0
        assert repository.job["state"] == "retryable"
        assert repository.job["error_code"] == "provision_transaction_timeout"

    asyncio.run(scenario())


@pytest.mark.parametrize("boundary", ("verification", "staging", "activation"))
def test_total_deadline_stops_delayed_verification_and_local_publication(monkeypatch, boundary):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    monotonic = [100.0]
    total_seconds = 1.0 if boundary == "verification" else 0.03
    monkeypatch.setattr(
        provisioning_service,
        "provision_deadline",
        lambda: ProvisionDeadline(
            provider_timeout_seconds=0.2,
            verification_grace_seconds=0.1,
            total_seconds=total_seconds,
        ),
    )

    class DelayedRepository(FakeRepository):
        async def stage_runtime_binding(self, *, uid, binding):
            if boundary == "staging":
                await asyncio.sleep(0.06)
            return await super().stage_runtime_binding(uid=uid, binding=binding)

        async def activate_runtime_binding(self, **kwargs):
            if boundary == "activation":
                await asyncio.sleep(0.06)
            return await super().activate_runtime_binding(**kwargs)

    if boundary == "verification":
        original_extract = provisioning_service.extract_runtime_binding

        def delayed_verification(*args, **kwargs):
            result = original_extract(*args, **kwargs)
            monotonic[0] += 2.0
            return result

        monkeypatch.setattr(provisioning_service, "extract_runtime_binding", delayed_verification)

    repository = DelayedRepository()
    coordinator = ProvisioningCoordinator(
        repository,
        FakeProvisionClient(_runtime_receipt()),
        monotonic_clock=lambda: monotonic[0],
    )
    asyncio.run(
        coordinator.process_claimed_job(
            job=_job(state="provisioning", stage="profile_ready"),
            identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
        )
    )

    assert repository.job["state"] == "retryable"
    assert repository.job["error_code"] == "provision_transaction_timeout"
    assert repository.activation_calls == 0
    assert not any(call["state"] == "ready" for call in repository.job_calls)
    if boundary in {"verification", "staging"}:
        assert repository.staged is None
    else:
        assert repository.staged["active"] is False


def test_external_cancellation_after_provider_work_records_retryable_reconciliation(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")

    class CancellableProvisionClient(FakeProvisionClient):
        def __init__(self):
            super().__init__(_runtime_receipt())
            self.started = asyncio.Event()

        async def provision(self, identity, target_schema_version, *, attestation_challenge):
            self.calls.append((identity, target_schema_version, attestation_challenge))
            self.started.set()
            await asyncio.Event().wait()

    async def scenario():
        repository = FakeRepository()
        client = CancellableProvisionClient()
        coordinator = ProvisioningCoordinator(repository, client)
        task = asyncio.create_task(
            coordinator.process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles"),
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert repository.staged is None
        assert repository.activation_calls == 0
        assert repository.job["state"] == "retryable"
        assert repository.job["error_code"] == "provision_transaction_cancelled"
        assert repository.job_calls[-1]["receipt"] == {
            "type": "runtime_attestation_reconciliation_required",
            "same_job_binding_required": True,
            "content_free": True,
        }

    asyncio.run(scenario())


def test_post_provider_attestation_rejections_retry_and_reconcile_one_binding(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")

    for failure in ("stale", "malformed", "context", "mac"):
        clock = [1_700_000_000.0]

        class RejectedProofClient(FakeProvisionClient):
            async def provision(self, requested_identity, target_schema_version, *, attestation_challenge):
                self.calls.append((requested_identity, target_schema_version, attestation_challenge))
                receipt = _attach_attestation(copy.deepcopy(self.result), attestation_challenge)
                if failure == "stale":
                    clock[0] = attestation_challenge["expires_at"] + 1
                elif failure == "malformed":
                    receipt["honchoAttestation"].pop("nonce")
                elif failure == "context":
                    receipt["honchoAttestation"]["firebase_uid"] = "user-b"
                    receipt["honchoAttestation"]["signature"] = calculate_signature(receipt["honchoAttestation"])
                else:
                    receipt["honchoAttestation"]["signature"] = "0" * 64
                return receipt

        repository = FakeRepository()
        client = RejectedProofClient(_runtime_receipt())
        asyncio.run(
            ProvisioningCoordinator(repository, client, clock=lambda: clock[0]).process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=identity,
            )
        )
        assert repository.job["state"] == "retryable"
        assert repository.job["retryable"] is True
        assert repository.staged is None
        assert repository.activation_calls == 0

    clock = [1_700_000_000.0]

    class RecoveringProofClient(FakeProvisionClient):
        async def provision(self, requested_identity, target_schema_version, *, attestation_challenge):
            self.calls.append((requested_identity, target_schema_version, attestation_challenge))
            receipt = _attach_attestation(copy.deepcopy(self.result), attestation_challenge)
            clock[0] += 1
            if len(self.calls) == 1:
                receipt["honchoAttestation"]["signature"] = "0" * 64
            return receipt

    repository = FakeRepository()
    client = RecoveringProofClient(_runtime_receipt())
    coordinator = ProvisioningCoordinator(repository, client, clock=lambda: clock[0])
    job = _job(state="provisioning", stage="profile_ready")
    asyncio.run(coordinator.process_claimed_job(job=job, identity=identity))
    assert repository.job["state"] == "retryable"
    assert repository.staged is None
    asyncio.run(coordinator.process_claimed_job(job=job, identity=identity))
    assert repository.job["state"] == "ready"
    assert repository.activation_calls == 1
    assert len(client.calls) == 2
    first_challenge, second_challenge = client.calls[0][2], client.calls[1][2]
    assert first_challenge["nonce"] != second_challenge["nonce"]
    assert first_challenge["job_id"] == second_challenge["job_id"]
    assert first_challenge["binding_id"] == second_challenge["binding_id"]
    assert provision_idempotency_key(first_challenge) == provision_idempotency_key(second_challenge)
    assert deterministic_runtime_binding_id(uid="user-a", provider="hermes", role="user") == (
        deterministic_runtime_binding_id(uid="user-a", provider="hermes", role="user")
    )
    assert deterministic_runtime_binding_id(uid="user-a", provider="hermes", role="user") != (
        deterministic_runtime_binding_id(uid="user-b", provider="hermes", role="user")
    )


def test_omi_identity_defaults_do_not_grant_cloud_or_recording_permission():
    document = _FakeDocument(exists=True, data={"onboarding": {"completed": True}})
    repository = EllaProvisioningRepository(pool=None, firestore_db=_FakeFirestore(document))

    changed = asyncio.run(
        repository.ensure_omi_user_document(
            uid="user-a",
            email="a@example.com",
            name="A",
            timezone_name="America/Los_Angeles",
        )
    )

    assert changed is True
    payload, merge = document.writes[0]
    assert merge is True
    assert payload["private_cloud_sync_enabled"] is False
    assert payload["store_recording_permission"] is False


def test_legacy_omi_identity_repair_preserves_cloud_sync_default():
    document = _FakeDocument(exists=True, data={"onboarding": {"completed": True}})
    repository = EllaProvisioningRepository(pool=None, firestore_db=_FakeFirestore(document))

    changed = asyncio.run(
        repository.ensure_omi_user_document(
            uid="legacy-user",
            email="legacy@example.com",
            name="Legacy",
            timezone_name="America/Los_Angeles",
            private_cloud_sync_default=True,
        )
    )

    assert changed is True
    payload, merge = document.writes[0]
    assert merge is True
    assert payload["private_cloud_sync_enabled"] is True
    assert payload["store_recording_permission"] is False


def test_repository_schema_preflight_reports_missing_objects():
    pool = _FakeSchemaPool(
        {
            "jobs_table": True,
            "bindings_table": False,
            "missing_indexes": ["ella_runtime_bindings_one_active_role_key"],
        }
    )
    repository = EllaProvisioningRepository(pool)

    with pytest.raises(ProvisioningSchemaNotReadyError) as error:
        asyncio.run(repository.assert_schema_ready())

    assert error.value.missing == (
        "table:ella_runtime_bindings",
        "index:ella_runtime_bindings_one_active_role_key",
    )
    assert len(pool.calls) == 1


def test_schema_preflight_runs_before_identity_mutation():
    repository = FakeRepository(schema_error=ProvisioningSchemaNotReadyError(["table:ella_provisioning_jobs"]))
    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")

    with pytest.raises(ProvisioningError, match="provisioning_schema_not_ready") as error:
        asyncio.run(
            coordinator.ensure_job(
                identity=identity,
                target_schema_version="hermes-user-v1",
                client_request_id="request-a",
                request_payload={"client": "ios"},
            )
        )

    assert error.value.retryable is True
    assert repository.schema_checks == 1
    assert repository.identity_calls == []


def test_self_hosted_invitation_admission_precedes_identity_and_job_writes(monkeypatch):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    identity = VerifiedIdentity("uninvited-user", "user@example.test", "User", "UTC")
    repository = FakeRepository(self_hosted_admission=None)
    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))

    with pytest.raises(ProvisioningError, match="invitation_authority_required"):
        asyncio.run(
            coordinator.ensure_job(
                identity=identity,
                target_schema_version="hermes-user-v1",
                client_request_id="request-uninvited",
                request_payload={"client": "ios"},
            )
        )

    assert repository.self_hosted_schema_checks == 1
    assert repository.identity_calls == []
    assert repository.job_calls == []

    repository.self_hosted_admission = _self_hosted_admission(identity.uid)
    job, binding, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-invited",
            request_payload={"client": "ios"},
        )
    )
    assert binding is None
    assert claimed is True
    assert job["state"] == "provisioning"
    assert len(repository.identity_calls) == 1
    assert len(repository.job_calls) == 1


def test_self_hosted_authority_drift_blocks_before_provider_call(monkeypatch):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    identity = VerifiedIdentity("invited-user", "user@example.test", "User", "UTC")
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", identity.uid)

    for drift in ("revoked", "target", "consent"):
        repository = FakeRepository(self_hosted_admission=None, self_hosted_owned=True)
        repository.authority_drift = drift
        client = FakeProvisionClient(_runtime_receipt())
        coordinator = ProvisioningCoordinator(repository, client)

        with pytest.raises(ProvisioningError, match="invitation_authority_required"):
            asyncio.run(
                coordinator.ensure_job(
                    identity=identity,
                    target_schema_version="hermes-user-v1",
                    client_request_id=f"request-{drift}",
                    request_payload={"client": "ios", "drift": drift},
                )
            )
        assert repository.identity_calls == []
        assert repository.job_calls == []

        repository.job = _job(state="provisioning", stage="profile_ready")
        asyncio.run(coordinator.process_claimed_job(job=dict(repository.job), identity=identity))
        assert client.calls == []
        assert repository.job["state"] == "blocked"
        assert repository.job["error_code"] == "invitation_authority_required"

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_HERMES_PROVISIONING_ENABLED_UIDS", raising=False)
    repository = FakeRepository(self_hosted_admission=None, self_hosted_owned=True)

    assert asyncio.run(runtime_authority_enabled(identity.uid, repository=repository)) is True
    with pytest.raises(ProvisioningError, match="self_hosted_invitation_runtime_disabled") as disabled:
        asyncio.run(
            resolve_isolated_runtime(
                identity.uid,
                repository=repository,
                target_mode="hermes-chat",
            )
        )
    assert disabled.value.retryable is True


def test_public_receipt_does_not_expose_runtime_secrets():
    receipt = public_receipt(
        _job(state="ready", stage="active", retryable=False),
        {
            "active": True,
            "revision": 7,
            "model_policy_version": "frontier-v1",
            "voice_policy_version": "ella-voice-v1",
            "credential_ref": "env:TOP_SECRET",
            "internal_gateway_url": "http://100.76.138.56:8701",
            "workspace_root": "/private/workspace",
        },
    )

    assert receipt["state"] == "ready"
    assert receipt["binding_revision"] == 7
    serialized = str(receipt)
    assert "TOP_SECRET" not in serialized
    assert "100.76.138.56" not in serialized
    assert "/private/workspace" not in serialized


def test_retained_compatibility_receipt_is_operational_and_credential_free():
    receipt = retained_compatibility_receipt("hermes-user-v1")

    assert receipt["state"] == "ready"
    assert receipt["binding_state"] == "active"
    assert receipt["binding_revision"] > 0
    assert receipt["effective_policy_revision"]
    assert receipt["compatibility_mode"] == "retained"
    serialized = str(receipt).lower()
    for forbidden in ("credential", "token", "gateway", "workspace", "http"):
        assert forbidden not in serialized


def test_gateway_credentials_are_server_env_references_only(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "secret-value")
    assert resolve_gateway_credential("env:ELLA_HERMES_GATEWAY_KEY_USER_A") == "secret-value"
    for invalid in (None, "secret-value", "file:/tmp/key", "env:PATH", "env:OPENAI_API_KEY"):
        with pytest.raises(ProvisioningError, match="invalid_credential_reference"):
            resolve_gateway_credential(invalid)


def test_retained_owner_identity_is_exact_and_requires_server_configuration(monkeypatch):
    monkeypatch.delenv("ELLA_PLATO_UID", raising=False)
    assert retained_owner_uid_configured("owner-uid") is False
    monkeypatch.setenv("ELLA_PLATO_UID", "owner-uid")
    assert retained_owner_uid_configured("owner-uid") is True
    assert retained_owner_uid_configured("OWNER-UID") is False
    assert retained_owner_uid_configured(" owner-uid ") is False


def test_internal_gateway_is_limited_to_loopback_tailnet_or_allowlist(monkeypatch):
    assert validate_internal_gateway_url("http://127.0.0.1:8701") == "http://127.0.0.1:8701"
    assert validate_internal_gateway_url("http://100.76.138.56:8701/") == "http://100.76.138.56:8701"
    with pytest.raises(ProvisioningError, match="invalid_internal_gateway_url"):
        validate_internal_gateway_url("https://example.com/runtime")
    for invalid in (
        "http://100.76.138.56:8701/admin",
        "http://100.76.138.56:8701/?token=secret",
        "http://100.76.138.56:8701/#fragment",
    ):
        with pytest.raises(ProvisioningError, match="invalid_internal_gateway_url"):
            validate_internal_gateway_url(invalid)
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_ALLOWED_HOSTS", "hermes.internal")
    assert validate_internal_gateway_url("http://hermes.internal:8701") == "http://hermes.internal:8701"


def test_runtime_receipt_rejects_non_hermes_and_plato_cross_user(monkeypatch):
    non_hermes = _runtime_receipt()
    non_hermes["runtimeBinding"]["provider"] = "openclaw"
    with pytest.raises(ProvisioningError, match="invalid_runtime_provider"):
        _extract(non_hermes)

    monkeypatch.setenv("ELLA_PLATO_UID", "plato-owner")
    with pytest.raises(ProvisioningError, match="plato_binding_forbidden"):
        _extract(_runtime_receipt("plato-eval"))
    plato_challenge = _attestation_challenge("plato-owner")
    assert (
        _extract(
            _runtime_receipt("plato-eval", plato_challenge),
            "plato-owner",
            challenge=plato_challenge,
        )["profile_name"]
        == "plato-eval"
    )


def test_runtime_receipt_requires_owned_workspace_port_and_honcho():
    wrong_workspace = _runtime_receipt()
    wrong_workspace["runtimeBinding"]["workspaceRoot"] = "/Users/ellaai/.hermes/profiles/plato-eval/workspace"
    with pytest.raises(ProvisioningError, match="workspace_ownership_mismatch"):
        _extract(wrong_workspace)

    wrong_port = _runtime_receipt()
    wrong_port["runtimeBinding"]["gatewayPort"] = 8702
    with pytest.raises(ProvisioningError, match="gateway_port_mismatch"):
        _extract(wrong_port)

    no_honcho = _runtime_receipt()
    no_honcho["runtimeBinding"]["honcho"] = {}
    with pytest.raises(ProvisioningError, match="honcho_receipt_incomplete"):
        _extract(no_honcho)

    no_runtime_proof = _runtime_receipt()
    no_runtime_proof.pop("honchoProfileMap")
    with pytest.raises(ProvisioningError, match="honcho_runtime_proof_incomplete"):
        _extract(no_runtime_proof)

    wrong_runtime_proof = _runtime_receipt()
    wrong_runtime_proof["provisioningReceipt"]["honcho"]["validation"][
        "configPath"
    ] = "/Users/ellaai/.hermes/profiles/plato-eval/honcho.json"
    with pytest.raises(ProvisioningError, match="honcho_runtime_proof_mismatch"):
        _extract(wrong_runtime_proof)

    unvalidated_runtime_proof = _runtime_receipt()
    unvalidated_runtime_proof["provisioningReceipt"]["honcho"]["validation"]["ok"] = False
    with pytest.raises(ProvisioningError, match="honcho_runtime_proof_mismatch"):
        _extract(unvalidated_runtime_proof)

    cross_tenant_runtime_proof = _runtime_receipt()
    cross_tenant_runtime_proof["honchoProfileMap"]["target"]["workspace"] = "plato-workspace"
    with pytest.raises(ProvisioningError, match="honcho_runtime_proof_mismatch"):
        _extract(cross_tenant_runtime_proof)

    wrong_peer_runtime_proof = _runtime_receipt()
    wrong_peer_runtime_proof["provisioningReceipt"]["honcho"]["validation"]["target"][
        "observer_peer_id"
    ] = "plato-observer"
    with pytest.raises(ProvisioningError, match="honcho_runtime_proof_mismatch"):
        _extract(wrong_peer_runtime_proof)

    with pytest.raises(ProvisioningError, match="runtime_template_version_mismatch"):
        _extract(_runtime_receipt(), expected_template_version="hermes-user-v2")


@pytest.mark.parametrize("invalid_value", [False, "false", "0", 0, 1, None])
def test_runtime_receipt_requires_strict_boolean_smoke_evidence(invalid_value):
    receipt = _runtime_receipt()
    receipt["runtimeBinding"]["smokePassed"] = invalid_value

    with pytest.raises(ProvisioningError, match="runtime_smoke_incomplete"):
        _extract(receipt)


def test_runtime_receipt_rejects_conflicting_smoke_evidence():
    receipt = _runtime_receipt()
    receipt["runtimeBinding"]["healthReceipt"]["smoke_passed"] = False

    with pytest.raises(ProvisioningError, match="runtime_smoke_incomplete"):
        _extract(receipt)


def test_runtime_receipt_rejects_conflicting_camel_case_health_smoke_evidence():
    receipt = _runtime_receipt()
    receipt["runtimeBinding"]["healthReceipt"]["smokePassed"] = False

    with pytest.raises(ProvisioningError, match="runtime_smoke_incomplete"):
        _extract(receipt)


def test_authenticated_honcho_attestation_rejects_replay_swaps_staleness_and_partial_proofs():
    tenant_a_challenge = _attestation_challenge("user-a")
    receipt = _runtime_receipt(challenge=tenant_a_challenge)
    binding = _extract(receipt, challenge=tenant_a_challenge)
    evidence = binding["health_receipt"]["honcho_isolation"]
    attestation = evidence["attestation"]
    assert attestation["firebase_uid"] == "user-a"
    assert attestation["account_owner_id"] == "22222222-2222-2222-2222-222222222222"
    assert attestation["gateway_port"] == 8701
    assert attestation["binding_id"] == "44444444-4444-4444-4444-444444444444"
    assert attestation["job_id"] == "11111111-1111-1111-1111-111111111111"
    assert "config_path_sha256" in attestation
    assert "/Users/ellaai/.hermes/profiles" not in str(evidence)
    assert "internalGatewayUrl" not in str(evidence)
    assert "credentialRef" not in str(evidence)

    tenant_b_challenge = _attestation_challenge("tenant-b")
    with pytest.raises(ProvisioningError, match="honcho_attestation_context_mismatch") as replay_error:
        _extract(receipt, "tenant-b", challenge=tenant_b_challenge)
    assert replay_error.value.retryable is True

    for field, replacement in (
        ("runtime_target_id", "target-swapped"),
        ("binding_id", "55555555-5555-5555-5555-555555555555"),
        ("job_id", "66666666-6666-6666-6666-666666666666"),
    ):
        swapped = dict(tenant_a_challenge, **{field: replacement})
        with pytest.raises(ProvisioningError, match="honcho_attestation_context_mismatch"):
            _extract(receipt, challenge=swapped)

    port_swap = copy.deepcopy(receipt)
    port_swap["runtimeBinding"]["gatewayPort"] = 8702
    port_swap["runtimeBinding"]["internalGatewayUrl"] = "http://100.76.138.56:8702"
    with pytest.raises(ProvisioningError, match="honcho_attestation_readback_mismatch"):
        _extract(port_swap, challenge=tenant_a_challenge)

    with pytest.raises(ProvisioningError, match="honcho_attestation_stale") as stale_error:
        _extract(receipt, challenge=tenant_a_challenge, now=tenant_a_challenge["expires_at"] + 1)
    assert stale_error.value.retryable is True

    partial = copy.deepcopy(receipt)
    partial["honchoAttestation"].pop("nonce")
    with pytest.raises(ProvisioningError, match="honcho_attestation_malformed") as malformed_error:
        _extract(partial, challenge=tenant_a_challenge)
    assert malformed_error.value.retryable is True

    malformed_nonce_challenge = dict(tenant_a_challenge, nonce="short")
    with pytest.raises(ProvisioningError, match="honcho_attestation_freshness_invalid"):
        _extract(
            _runtime_receipt(challenge=malformed_nonce_challenge),
            challenge=malformed_nonce_challenge,
        )

    forged = copy.deepcopy(receipt)
    forged["honchoAttestation"]["signature"] = "0" * 64
    with pytest.raises(ProvisioningError, match="honcho_attestation_integrity_invalid") as integrity_error:
        _extract(forged, challenge=tenant_a_challenge)
    assert integrity_error.value.retryable is True


def test_missing_attestation_key_fails_before_provisioner_or_binding_write(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    for invalid_key in (
        None,
        "",
        " " * 32,
        "short",
        " unit-test-attestation-key-32-bytes-minimum",
        "unit-test-attestation-key-32-bytes-minimum ",
        "unit-test-attestation key-32-bytes-minimum",
    ):
        if invalid_key is None:
            monkeypatch.delenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY")
        else:
            monkeypatch.setenv("ELLA_HERMES_PROVISION_ATTESTATION_KEY", invalid_key)
        repository = FakeRepository()
        client = FakeProvisionClient({"mode": "hermes_only", "provisionMode": "hermes_only"})
        identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")

        asyncio.run(
            ProvisioningCoordinator(repository, client).process_claimed_job(
                job=_job(state="provisioning", stage="profile_ready"),
                identity=identity,
            )
        )

        assert client.calls == []
        assert repository.staged is None
        assert repository.job["error_code"] == "honcho_attestation_key_unavailable"


def test_fresh_uid_relax_admits_uninvited_self_hosted_when_flag_set(monkeypatch):
    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "true")
    identity = VerifiedIdentity("fresh-relax-user", "user@example.test", "User", "UTC")
    repository = FakeRepository(self_hosted_admission=None, self_hosted_owned=False)

    # Without the relax flag a fresh self-hosted UID is denied (strict gate).
    assert self_hosted_provisioning_enabled(identity.uid, admission=None) is False

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", "true")
    assert self_hosted_fresh_uid_relax_enabled() is True
    assert self_hosted_provisioning_enabled(identity.uid, admission=None) is True

    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))
    job, binding, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-fresh-relax",
            request_payload={"client": "ios"},
        )
    )
    # Fresh user admitted: proceeds past the gate without invitation_authority_required.
    assert job["state"] in {"provisioning", "queued"}
    assert claimed is True
    assert len(repository.identity_calls) == 1
    assert len(repository.job_calls) == 1

    # Strict matching still enforced when an admission record IS present.
    monkeypatch.delenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", raising=False)
    repository.self_hosted_admission = _self_hosted_admission(identity.uid)
    assert self_hosted_provisioning_enabled(identity.uid, admission=repository.self_hosted_admission) is True
    repository.self_hosted_admission.pop("consent_scope_hash", None)
    repository.self_hosted_admission["consent_scope_hash"] = "sha256:deadbeef"
    assert self_hosted_provisioning_enabled(identity.uid, admission=repository.self_hosted_admission) is False

    monkeypatch.setenv("ELLA_SELF_HOSTED_PROVISIONING_ENABLED", "false")
    monkeypatch.delenv("ELLA_SELF_HOSTED_PROVISIONING_RELAX_FRESH_UID", raising=False)


def test_runtime_resolver_enforces_owner_health_and_credential(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "secret-value")
    binding = _extract(_runtime_receipt())
    binding.update(omi_uid="user-a", active=True, revision=4)
    runtime = runtime_from_binding(binding, "user-a")
    assert runtime.profile_name == "omi-user-a"
    assert runtime.gateway_token == "secret-value"
    with pytest.raises(ProvisioningError, match="runtime_ownership_mismatch"):
        runtime_from_binding(binding, "user-b")

    invalid_honcho = dict(binding, honcho_workspace="")
    with pytest.raises(ProvisioningError, match="honcho_receipt_incomplete"):
        runtime_from_binding(invalid_honcho, "user-a")

    invalid_workspace = dict(binding, workspace_root="/Users/ellaai/.hermes/profiles/another-user/workspace")
    with pytest.raises(ProvisioningError, match="workspace_ownership_mismatch"):
        runtime_from_binding(invalid_workspace, "user-a")


def test_invitation_runtime_requires_persisted_profile_local_honcho_proof(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_GATEWAY_KEY_USER_A", "secret-value")
    binding = _extract(_runtime_receipt())
    binding.update(
        id="44444444-4444-4444-4444-444444444444",
        omi_uid="user-a",
        active=True,
        revision=4,
        runtime_target_id="target-a",
        attestation_runtime_target_id="33333333-3333-3333-3333-333333333333",
        runtime_target_mode="hermes-chat",
        target_policy_version=CURRENT_POLICY_VERSION,
        target_processor_set_hash=CURRENT_PROCESSOR_SET_HASH,
        target_scope_version=CURRENT_SCOPE_VERSION,
        target_scope_hash=CURRENT_SCOPE_HASH,
        consent_authority_epoch="11111111-1111-1111-1111-111111111111",
        account_user_id="22222222-2222-2222-2222-222222222222",
        profile_user_id="22222222-2222-2222-2222-222222222222",
    )
    assert runtime_from_binding(binding, "user-a").profile_name == "omi-user-a"

    missing = dict(binding, health_receipt={"smoke_passed": True})
    with pytest.raises(ProvisioningError, match="honcho_attestation_evidence_malformed"):
        runtime_from_binding(missing, "user-a")

    mismatched_receipt = copy.deepcopy(binding["health_receipt"])
    mismatched_receipt["honcho_isolation"]["attestation"]["honcho_workspace"] = "plato-workspace"
    mismatched = dict(binding, health_receipt=mismatched_receipt)
    with pytest.raises(ProvisioningError, match="honcho_attestation_readback_mismatch"):
        runtime_from_binding(mismatched, "user-a")


def test_disabled_provisioning_stays_retryable_and_can_resume(monkeypatch):
    repository = FakeRepository()
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "false")
    job, binding, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )
    assert (job["state"], job["retryable"], claimed, binding) == ("degraded", True, False, None)

    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    job, _, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-b",
            request_payload={"client": "ios"},
        )
    )
    assert job["state"] == "provisioning"
    assert claimed is True


def test_omi_identity_failure_is_durable_and_does_not_call_hermes(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    repository = FakeRepository(omi_identity_error=RuntimeError("firestore unavailable"))
    client = FakeProvisionClient(_runtime_receipt())
    coordinator = ProvisioningCoordinator(repository, client)
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")

    job, binding, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )

    assert (job["state"], job["error_code"], binding, claimed) == (
        "degraded",
        "omi_identity_unavailable",
        None,
        False,
    )
    assert client.calls == []


def test_existing_binding_reconciles_pending_user_to_active(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    binding = {
        "revision": 4,
        "user_status": "PENDING",
        "active": True,
        "template_version": "hermes-user-v1",
    }
    repository = FakeRepository(binding=binding)
    coordinator = ProvisioningCoordinator(repository, FakeProvisionClient(_runtime_receipt()))
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")

    job, resolved, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )

    assert (job["state"], resolved, claimed) == ("ready", binding, False)
    assert repository.user_active is True


def test_successful_provision_stages_then_activates_binding(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    repository = FakeRepository()
    client = FakeProvisionClient(_runtime_receipt())
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(repository, client)
    job, _, claimed = asyncio.run(
        coordinator.ensure_job(
            identity=identity,
            target_schema_version="hermes-user-v1",
            client_request_id="request-a",
            request_payload={"client": "ios"},
        )
    )
    assert claimed is True

    asyncio.run(coordinator.process_claimed_job(job=job, identity=identity))

    assert repository.job["state"] == "ready"
    assert repository.binding["active"] is True
    assert repository.binding["provider"] == "hermes"
    assert repository.user_active is True
    assert repository.omi_identity_calls == [
        {
            "uid": "user-a",
            "email": "a@example.com",
            "name": "A",
            "timezone_name": "America/Los_Angeles",
        }
    ]
    assert len(client.calls) == 1
    assert client.calls[0][:2] == (identity, "hermes-user-v1")
    assert client.calls[0][2]["firebase_uid"] == identity.uid


def test_legacy_8210_receipt_cannot_activate(monkeypatch):
    monkeypatch.setenv("ELLA_HERMES_PROVISIONING_ENABLED", "true")
    repository = FakeRepository()
    identity = VerifiedIdentity("user-a", "a@example.com", "A", "America/Los_Angeles")
    coordinator = ProvisioningCoordinator(
        repository,
        FakeProvisionClient({"mode": "hermes_only", "provisionMode": "hermes_only"}),
    )

    asyncio.run(coordinator.process_claimed_job(job=_job(state="provisioning"), identity=identity))

    assert repository.job["state"] == "retryable"
    assert repository.job["retryable"] is True
    assert repository.job["error_code"] == "runtime_receipt_missing"
    assert repository.binding is None
