import ast
import asyncio
import copy
import re
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
    for relative_root in ("database", "ella", "utils/ella"):
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
        lambda: ProvisionDeadline(provider_timeout_seconds=0.3, verification_grace_seconds=0.01, total_seconds=0.12),
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
                    await asyncio.sleep(0.025)
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
            await asyncio.wait_for(request_received.wait(), timeout=0.2)
            await asyncio.wait_for(response_finished.wait(), timeout=0.2)

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
