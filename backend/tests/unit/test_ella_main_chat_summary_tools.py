"""Local-only canonical CAS coverage using the corrections router's isolated imports."""

import asyncio
import copy
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from test_ella_conversation_corrections import corrections, summary_writeback, summary_recovery
from utils.ella.canonical_omi import transcript_grounding_hash


@pytest.fixture
def operation(monkeypatch):
    state = {
        "id": "chosen-memory",
        "status": "completed",
        "discarded": False,
        "transcript_segments": [{"speaker": "User", "text": "The podcast covered gardening."}],
        "structured": {
            "title": "podcast",
            "overview": "[Ella] gardening techniques were discussed in detail.",
            "emoji": "leaf",
            "category": "other",
        },
        "active_summary_version_id": "before-v1",
        "summary_versions": [
            {
                "id": "before-v1",
                "title": "podcast",
                "overview": "[Ella] gardening techniques were discussed in detail.",
                "emoji": "leaf",
                "category": "other",
                "is_active": True,
            }
        ],
        "apps_results": [{"kept": True}],
    }
    original = copy.deepcopy(state)
    audits, provider_calls, writes = {}, [], []
    publication = {"fail": False}
    runtime = SimpleNamespace(
        uid="owner",
        binding_id="binding-1",
        profile_user_id="profile-1",
        account_user_id="account-1",
        provider="hermes",
        gateway_url="https://owner.test",
        agent_id="owner-agent",
        gateway_token="fake",
    )

    async def resolve(uid, **kwargs):
        assert uid == "owner"
        return runtime

    async def revalidate(identity):
        return runtime

    async def generate(**kwargs):
        provider_calls.append(kwargs)
        await asyncio.sleep(0)
        return {
            "title": "podcast gardening",
            "overview": "[Ella] the podcast discussed practical gardening techniques.",
            "emoji": "leaf",
            "category": "other",
            "ella_tags": ["omi"],
            "ella_signal": {},
        }

    def get(uid, cid):
        return copy.deepcopy(state) if uid == "owner" and cid == "chosen-memory" else None

    def update(uid, cid, fields):
        assert (uid, cid) == ("owner", "chosen-memory")
        for key, value in fields.items():
            if key.startswith("structured."):
                state["structured"][key.split(".", 1)[1]] = copy.deepcopy(value)
            else:
                state[key] = copy.deepcopy(value)

    def versions(conversation, *, next_structured, source, kind, correction_id, based_on_version_id, activate):
        result = copy.deepcopy(conversation["summary_versions"])
        for version in result:
            version["is_active"] = False
        version_id = f"version-{len(result) + 1}"
        result.append(
            {
                **next_structured,
                "id": version_id,
                "source": source,
                "kind": kind,
                "correction_id": correction_id,
                "based_on_version_id": based_on_version_id,
                "is_active": activate,
            }
        )
        return {
            "summary_versions": result,
            "active_summary_version_id": version_id,
            "new_summary_version_id": version_id,
        }

    def source_cas(
        uid, cid, expected_hash, fields, *, expected_active_summary_version_id, match_active_summary_version
    ):
        assert match_active_summary_version
        if (
            expected_hash != transcript_grounding_hash(state["transcript_segments"])
            or state["active_summary_version_id"] != expected_active_summary_version_id
        ):
            return False
        writes.append(copy.deepcopy(fields))
        update(uid, cid, fields)
        return True

    def authority_cas(uid, cid, expected_version, expected_state, fields):
        if (
            state["active_summary_version_id"] != expected_version
            or state.get("enrichment_state", {}) != expected_state
        ):
            return False
        update(uid, cid, fields)
        return True

    async def apply(**kwargs):
        summary = kwargs.pop("summary")
        kwargs["based_on_version_id"] = kwargs.pop("active_summary_version_id")
        return await summary_writeback.write_conversation_summary(
            **kwargs,
            **{key: summary.get(key) for key in ("title", "overview", "emoji", "category", "ella_tags", "ella_signal")},
            canonical_writer=lambda *args, **kw: {"ok": not publication["fail"]},
        )

    def audit_ref(uid, cid, correction_id):
        return SimpleNamespace(
            get=lambda: SimpleNamespace(exists=correction_id in audits, to_dict=lambda: audits.get(correction_id, {}))
        )

    monkeypatch.setattr(corrections, "assert_current_ai_consent", lambda uid: uid)
    monkeypatch.setattr(corrections, "require_isolated_runtime", resolve)
    monkeypatch.setattr(
        corrections, "runtime_authority_identity", lambda value: SimpleNamespace(uid=value.uid, digest="binding")
    )
    monkeypatch.setattr(corrections, "revalidate_runtime_authority", revalidate)
    monkeypatch.setattr(corrections, "generate_summary_from_prompt", generate)
    monkeypatch.setattr(corrections, "get_user_from_uid", lambda uid: None)
    monkeypatch.setattr(corrections, "apply_summary_update", apply)
    monkeypatch.setattr(corrections, "_audit_ref", audit_ref)
    monkeypatch.setattr(
        corrections, "_persist_correction_audit", lambda uid, cid, key, data: audits.setdefault(key, {}).update(data)
    )
    monkeypatch.setattr(corrections, "_correction_propagation_counts", lambda *args: (0, 0, "known"))
    monkeypatch.setattr(corrections, "_prepare_applied_propagation_rollbacks", lambda *args: [])
    monkeypatch.setattr(corrections.conversations_db, "get_conversation", get)
    monkeypatch.setattr(corrections.conversations_db, "update_conversation", update)
    monkeypatch.setattr(summary_writeback.conversations_db, "get_conversation", get)
    monkeypatch.setattr(summary_writeback.conversations_db, "build_summary_version_update", versions)
    monkeypatch.setattr(summary_writeback.conversations_db, "update_conversation_if_transcript_hash", source_cas)
    monkeypatch.setattr(summary_writeback.conversations_db, "update_conversation_if_summary_authority", authority_cas)
    return SimpleNamespace(
        state=state,
        original=original,
        audits=audits,
        provider_calls=provider_calls,
        writes=writes,
        runtime=runtime,
        publication=publication,
    )


def request(correction=True, **overrides):
    fields = {"expected_active_summary_version_id": "before-v1", "idempotency_key": "invocation-1", **overrides}
    if correction:
        fields.setdefault("correction_text", "It was the podcast, not the user.")
        return corrections.ConversationCorrectionRequest(**fields)
    return corrections.ConversationResummaryRequest(**fields)


async def run(payload, **kwargs):
    return await corrections.run_explicit_summary_operation(
        uid="owner", conversation_id="chosen-memory", request=payload, **kwargs
    )


@pytest.mark.parametrize("correction", [True, False])
def test_exact_success_receipt_and_undo_preserve_transcript_history(operation, correction):
    result = asyncio.run(run(request(correction)))
    assert result["receipt"]["before_version_id"] == "before-v1"
    assert result["receipt"]["after_version_id"] == "version-2"
    assert result["undo_path"].endswith(f"/{result['correction_id']}/undo")
    assert operation.state["summary_versions"][-1]["summary_operation"]["intent"] == (
        "correction" if correction else "resummary"
    )
    assert operation.state["transcript_segments"] == operation.original["transcript_segments"]
    assert operation.state["apps_results"] == operation.original["apps_results"]
    prompt = operation.provider_calls[0]["prompt"]
    assert operation.provider_calls[0]["fallback"] == {}
    assert operation.provider_calls[0]["require_generated_summary_fields"] is True
    if not correction:
        assert "No factual correction was supplied" in prompt
        assert "User correction:" not in prompt
    undone = asyncio.run(
        corrections.undo_conversation_correction("chosen-memory", result["correction_id"], uid="owner")
    )
    assert undone.status == "undone"
    assert len(operation.state["summary_versions"]) == 3
    assert operation.state["structured"] == operation.original["structured"]
    assert operation.state["transcript_segments"] == operation.original["transcript_segments"]
    duplicate = asyncio.run(run(request(correction)))
    assert duplicate["receipt"]["status"] == "undone"
    assert len(operation.provider_calls) == 1


def test_concurrent_duplicate_commits_one_version(operation):
    async def duplicate():
        return await asyncio.gather(run(request()), run(request()))

    results = asyncio.run(duplicate())
    assert {result["correction_id"] for result in results} == {results[0]["correction_id"]}
    assert len(operation.state["summary_versions"]) == 2
    assert len(operation.writes) == 1
    # Provider execution is not exactly-once; the durable summary write is.
    assert len(operation.provider_calls) == 2
    asyncio.run(run(request()))
    assert len(operation.provider_calls) == 2


def test_idempotency_payload_change_rejected(operation):
    asyncio.run(run(request()))
    with pytest.raises(HTTPException) as error:
        asyncio.run(run(request(correction_text="Different correction.")))
    assert error.value.status_code == 409
    assert len(operation.writes) == 1


def test_canonical_failure_replay_repairs_receipt_without_provider_or_new_version(operation):
    operation.publication["fail"] = True
    with pytest.raises(summary_writeback.CanonicalSummaryWriteUnconfirmedError):
        asyncio.run(run(request()))
    assert len(operation.state["summary_versions"]) == 2
    assert len(operation.provider_calls) == 1
    operation.publication["fail"] = False
    result = asyncio.run(run(request()))
    assert result["idempotent_replay"] is True
    assert result["receipt"]["after_version_id"] == "version-2"
    assert operation.state["enrichment_state"]["canonical_status"] == "completed"
    assert len(operation.state["summary_versions"]) == 2
    assert len(operation.provider_calls) == 1


@pytest.mark.parametrize(
    "fault", ["stale", "wrong-owner", "no-binding", "consent", "provider", "transcript-race", "version-race"]
)
def test_fail_closed_without_summary_or_receipt_write(operation, monkeypatch, fault):
    payload = request()
    if fault == "stale":
        payload = request(expected_active_summary_version_id="stale-v0")
    elif fault == "wrong-owner":
        operation.runtime.uid = "different-owner"
    elif fault == "no-binding":
        operation.runtime.binding_id = ""
    elif fault == "consent":
        monkeypatch.setattr(
            corrections, "assert_current_ai_consent", lambda uid: (_ for _ in ()).throw(HTTPException(403, "revoked"))
        )
    else:

        async def fail(**kwargs):
            if fault == "provider":
                raise RuntimeError("provider failed")
            if fault == "transcript-race":
                operation.state["transcript_segments"].append({"text": "new capture segment"})
            else:
                operation.state["active_summary_version_id"] = "newer-v1"
            return {
                "title": "podcast",
                "overview": "[Ella] the podcast discussed practical gardening techniques.",
                "category": "other",
            }

        monkeypatch.setattr(corrections, "generate_summary_from_prompt", fail)
    with pytest.raises((HTTPException, RuntimeError)):
        asyncio.run(run(payload))
    assert operation.writes == []
    assert operation.audits == {}
    assert len(operation.state["summary_versions"]) == 1


def test_consent_revoked_while_provider_runs_blocks_write(operation, monkeypatch):
    permitted = True

    def consent(uid):
        if not permitted:
            raise HTTPException(403, "revoked")

    async def generate(**kwargs):
        nonlocal permitted
        permitted = False
        return {
            "title": "podcast",
            "overview": "[Ella] the podcast discussed practical gardening techniques.",
            "category": "other",
        }

    monkeypatch.setattr(corrections, "assert_current_ai_consent", consent)
    monkeypatch.setattr(corrections, "generate_summary_from_prompt", generate)
    with pytest.raises(HTTPException):
        asyncio.run(run(request()))
    assert operation.writes == []
    assert operation.audits == {}


@pytest.mark.parametrize(
    "generated", [{}, {"title": "unused"}, {"overview": "model omitted title"}, {"title": "unused", "overview": " "}]
)
def test_strict_provider_response_cannot_turn_fallback_into_generated_summary(generated):
    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def post(self, *args, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"choices": [{"message": {"content": json.dumps(generated)}}]},
            )

    config = summary_recovery.SummaryProviderConfig(
        provider="hermes-api",
        hermes_url="https://mock.test",
        hermes_model="mock",
        hermes_api_key="mock",
        legacy_url="",
        legacy_model="",
        legacy_api_key="",
        timeout_seconds=1,
    )
    with pytest.raises(ValueError, match="generated title or overview"):
        asyncio.run(
            summary_recovery.generate_summary_from_prompt(
                prompt="local mock",
                fallback={"title": "old summary", "overview": "old fallback summary must not be applied"},
                session_id="local",
                session_key="local",
                trace_id="local",
                required_tags=("omi",),
                config=config,
                async_client_factory=Client,
                require_generated_summary_fields=True,
            )
        )
