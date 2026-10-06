import asyncio
import hashlib
import json
import os
import uuid
from pathlib import Path
from unittest.mock import patch

import asyncpg
import pytest

from database.managed_cloud_consent import ManagedCloudGrant, consent_receipt_ref
from database import authority_advisory_lock
from database.standard_talk_playback_authority import ServerAssistantTurn, StandardTalkDenied
from ella.services import guardian_playback_ledger as ledger
from ella.services import runtime_resolver
from ella.services.ai_consent import CURRENT_POLICY_VERSION, SUPPORTED_CONSENT_POLICY_CONTRACTS
from ella.services.standard_talk_playback import FreshAuthority, StandardTalkPlaybackComposition, VerifiedSpokenText

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"
pytestmark = pytest.mark.skipif(not TEST_DSN, reason="approved disposable ELLA_TEST_POSTGRES_DSN required")

# Synthetic authority fixtures exercise the actual lock/query/decision methods.
# 008/018/020 are real migrations; this is not a full provisioning schema proof.
AUTHORITY_SCHEMA = """
CREATE TABLE users (id uuid PRIMARY KEY, omi_uid text UNIQUE, name text, status text, profile_class text);
CREATE TABLE ella_managed_cloud_consent_authority (
 user_id uuid PRIMARY KEY, decision text, consent_receipt_ref text, profile_binding_id text,
 policy_version text, processor_set_hash text, scope_version text, scope_hash text,
 authority_epoch uuid, revision integer);
CREATE TABLE ella_runtime_bindings (
 id uuid PRIMARY KEY, user_id uuid, account_user_id uuid, profile_user_id uuid,
 role text, provider text, status text, active boolean, health_state text,
 profile_name text, agent_id text, workspace_root text, internal_gateway_url text,
 gateway_port integer, credential_ref text, honcho_workspace text, observed_peer text,
 observer_peer text, revision integer, allowed_tools text[], required_capabilities text[],
 health_receipt jsonb, template_version text, model_policy_version text, voice_policy_version text);
CREATE TABLE ella_runtime_targets (
 id uuid PRIMARY KEY, account_user_id uuid, profile_user_id uuid, runtime_binding_id uuid,
 role text, provider text, status text, mode text, invitation_target_id uuid,
 endpoint_ref text, credential_ref text, entitlement_revision integer, updated_at timestamptz,
 policy_version text, processor_set_hash text, scope_version text, scope_hash text);
CREATE TABLE ella_invitations (
 id uuid PRIMARY KEY, kind text, state text, delivery_state text, required_consent_policy_version text,
 required_consent_processor_set_hash text, required_consent_scope_version text, required_consent_scope_hash text);
CREATE TABLE ella_invitation_targets (
 id uuid PRIMARY KEY, invitation_id uuid, required_profile_class text, consumed_at timestamptz, revoked_at timestamptz);
CREATE TABLE ella_invitation_redemptions (
 invitation_id uuid, invitation_target_id uuid, user_id uuid, user_mapping_state text, consent_pending boolean);
CREATE TABLE canonical_events (
 uid text, event_id text, source_identity text, session_id text, role text, channel text,
 provider text, privacy_scope text, scan_policy text, text text, canonical_identity text);
ALTER TABLE voice_entitlements ADD COLUMN invitation_id uuid,
 ADD COLUMN invitation_consent_pending boolean, ADD COLUMN consent_authority_epoch uuid,
 ADD COLUMN consent_authority_revision integer, ADD COLUMN consent_policy_version text,
 ADD COLUMN consent_processor_set_hash text, ADD COLUMN consent_scope_version text, ADD COLUMN consent_scope_hash text;
"""


async def with_database(scenario):
    schema = "talk_composition_" + uuid.uuid4().hex
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    pool = await asyncpg.create_pool(
        TEST_DSN, min_size=1, max_size=5, server_settings={"search_path": schema, "application_name": schema}
    )
    try:
        async with pool.acquire() as conn:
            await conn.execute((MIGRATIONS / "008_create_voice_canary_controls.sql").read_text())
            await conn.execute(AUTHORITY_SCHEMA)
            await conn.execute((MIGRATIONS / "018_create_guardian_playback_ledger.sql").read_text())
            await conn.execute((MIGRATIONS / "020_create_standard_talk_playback_references.sql").read_text())
        state = await seed(pool)
        state["schema"] = schema
        # Pure attestation/credential fixtures only: no gateway/provider/Firebase.
        with patch.object(runtime_resolver, "resolve_gateway_credential", return_value="synthetic-token"), patch.object(
            runtime_resolver, "verify_persisted_attestation"
        ):
            await scenario(pool, state)
    finally:
        await pool.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


async def seed(pool):
    owner, binding, target, invitation, invitation_target, epoch = [uuid.uuid4() for _ in range(6)]
    contract = SUPPORTED_CONSENT_POLICY_CONTRACTS[CURRENT_POLICY_VERSION]
    grant = ManagedCloudGrant(
        "synthetic",
        "synthetic",
        "receipt",
        "profile",
        contract.version,
        contract.processor_set_hash,
        contract.scope_version,
        contract.scope_hash,
    )
    model = runtime_resolver.SELF_HOSTED_RUNTIME_MODEL
    await pool.execute("INSERT INTO users VALUES ($1, 'synthetic', 'Synthetic', 'ACTIVE', 'real')", owner)
    await pool.execute(
        "INSERT INTO ella_managed_cloud_consent_authority VALUES ($1, 'granted', $2, $3, $4, $5, $6, $7, $8, 1)",
        owner,
        consent_receipt_ref("synthetic", "receipt"),
        grant.profile_binding_id,
        grant.policy_version,
        grant.processor_set_hash,
        grant.scope_version,
        grant.scope_hash,
        epoch,
    )
    await pool.execute(
        "INSERT INTO voice_entitlements (uid, status, revision, provider_allowlist, model_allowlist, mode_allowlist, fallback_policy, invitation_id, invitation_consent_pending, consent_authority_epoch, consent_authority_revision, consent_policy_version, consent_processor_set_hash, consent_scope_version, consent_scope_hash) VALUES ('synthetic', 'active', 1, ARRAY['hermes'], ARRAY[$1]::text[], ARRAY['hermes-chat','hermes-voice'], '{\"enabled\":false,\"order\":[]}', $2, FALSE, $3, 1, $4, $5, $6, $7)",
        model,
        invitation,
        epoch,
        grant.policy_version,
        grant.processor_set_hash,
        grant.scope_version,
        grant.scope_hash,
    )
    await pool.execute(
        "INSERT INTO ella_invitations VALUES ($1, 'ordinary', 'redeemed', 'sent', $2, $3, $4, $5)",
        invitation,
        grant.policy_version,
        grant.processor_set_hash,
        grant.scope_version,
        grant.scope_hash,
    )
    await pool.execute(
        "INSERT INTO ella_invitation_targets VALUES ($1, $2, 'real', NOW(), NULL)", invitation_target, invitation
    )
    await pool.execute(
        "INSERT INTO ella_invitation_redemptions VALUES ($1, $2, $3, 'mapped', FALSE)",
        invitation,
        invitation_target,
        owner,
    )
    health = json.dumps({"honcho_isolation": {"attestation": {"nonce": "synthetic", "job_id": "synthetic"}}})
    await pool.execute(
        "INSERT INTO ella_runtime_bindings VALUES ($1, $2, $2, $2, 'user', 'hermes', 'active', TRUE, 'healthy', 'synthetic-talk', 'synthetic-agent', '/Users/ellaai/.hermes/profiles/synthetic-talk/workspace', 'http://127.0.0.1:8123', 8123, 'synthetic-ref', 'synthetic-workspace', 'synthetic-observed', 'synthetic-observer', 1, ARRAY[]::text[], ARRAY[]::text[], $3::jsonb, 'template', 'model-policy', 'voice-policy')",
        binding,
        owner,
        health,
    )
    await pool.execute(
        "INSERT INTO ella_runtime_targets VALUES ($1,$2,$2,$3,'user','hermes','ready','hermes-chat',$4,'','',1,NOW(),$5,$6,$7,$8)",
        target,
        owner,
        binding,
        invitation_target,
        grant.policy_version,
        grant.processor_set_hash,
        grant.scope_version,
        grant.scope_hash,
    )
    turn = ServerAssistantTurn("synthetic", "turn", "session", hashlib.sha256(b"Hello synthetic world.").hexdigest())
    await pool.execute(
        "INSERT INTO canonical_events VALUES ($1,$2,$3,$4,'assistant','ios_chat','omi-ios-chat','user_private','none','Hello synthetic world.',$1)",
        turn.uid,
        turn.event_id,
        turn.source_identity,
        turn.session_id,
    )
    return dict(
        owner=owner,
        binding=binding,
        target=target,
        invitation_target=invitation_target,
        epoch=epoch,
        grant=grant,
        turn=turn,
        loader_calls=0,
    )


async def service(pool, state):
    async def loader(uid):
        state["loader_calls"] += 1
        if state.get("firestore_denied"):
            raise StandardTalkDenied("current_consent_denied")
        row = dict(
            await pool.fetchrow(
                "SELECT b.*, u.omi_uid, u.profile_class, t.id AS runtime_target_id, t.mode AS runtime_target_mode, t.updated_at AS runtime_target_updated_at, t.endpoint_ref AS target_endpoint_ref, t.credential_ref AS target_credential_ref, t.entitlement_revision AS target_entitlement_revision, t.invitation_target_id AS attestation_runtime_target_id, t.policy_version AS target_policy_version, t.processor_set_hash AS target_processor_set_hash, t.scope_version AS target_scope_version, t.scope_hash AS target_scope_hash, a.authority_epoch AS consent_authority_epoch FROM ella_runtime_bindings b JOIN users u ON u.id=b.user_id JOIN ella_runtime_targets t ON t.runtime_binding_id=b.id JOIN ella_managed_cloud_consent_authority a ON a.user_id=u.id"
            )
        )
        runtime = runtime_resolver.runtime_from_binding(
            row,
            uid,
            self_hosted_authority_lineage=runtime_resolver.RuntimeTargetLineage(
                state["grant"].policy_version,
                state["grant"].processor_set_hash,
                state["grant"].scope_version,
                state["grant"].scope_hash,
            ),
        )
        return FreshAuthority(runtime, state["grant"])

    def normalized(turn, raw):
        return VerifiedSpokenText(raw, turn.canonical_text_sha256, hashlib.sha256(raw.encode()).hexdigest())

    return StandardTalkPlaybackComposition(pool, fresh_authority=loader, verified_spoken_text=normalized, enabled=True)


def test_actual_composition_publishes_once_atomically_without_played_evidence():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        claims = await asyncio.gather(
            *[composition.claim_for_synthesis(reference.reference, state["turn"]) for _ in range(2)]
        )
        admitted = [item for item in claims if item is not None]
        assert len(admitted) == 1
        assert await composition.publish_generated(admitted[0])
        assert not await composition.publish_generated(admitted[0])
        assert await ledger.get_played_candidates(pool, "synthetic") == []
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 1
        assert state["loader_calls"] == 5
        assert await composition.issue_server_turn(state["turn"]) is None

    asyncio.run(with_database(scenario))


@pytest.mark.parametrize(
    "change",
    [
        "canonical_text",
        "role",
        "source",
        "session",
        "other_owner",
        "consent",
        "entitlement",
        "target",
        "invitation",
        "epoch_aba",
    ],
)
def test_late_change_denies_publication_and_leaves_no_ledger(change):
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        changes = {
            "canonical_text": "UPDATE canonical_events SET text='tampered'",
            "role": "UPDATE canonical_events SET role='user'",
            "source": "UPDATE canonical_events SET source_identity='other'",
            "session": "UPDATE canonical_events SET session_id='other'",
            "other_owner": "UPDATE canonical_events SET uid='other'",
            "consent": "UPDATE ella_managed_cloud_consent_authority SET decision='revoked'",
            "entitlement": "UPDATE voice_entitlements SET status='revoked'",
            "target": "UPDATE ella_runtime_targets SET status='revoked'",
            "invitation": "UPDATE ella_invitation_targets SET revoked_at=NOW()",
            "epoch_aba": "UPDATE ella_managed_cloud_consent_authority SET authority_epoch=gen_random_uuid(), revision=revision+1",
        }
        await pool.execute(changes[change])
        with pytest.raises(StandardTalkDenied):
            await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0
        assert await pool.fetchval("SELECT state FROM ella_standard_talk_playback_references") == "synthesizing"

    asyncio.run(with_database(scenario))


def test_ledger_failure_rolls_back_publication_and_caller_connection_does_not_reacquire():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])

        async def fail(*args, **kwargs):
            assert kwargs["connection"].is_in_transaction()
            raise RuntimeError("synthetic_write_failure")

        with patch.object(ledger, "record_generated", side_effect=fail):
            with pytest.raises(RuntimeError, match="synthetic_write_failure"):
                await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT state FROM ella_standard_talk_playback_references") == "synthesizing"
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0
        with patch.object(ledger, "_maybe_run_retention_cleanup", side_effect=AssertionError("second pool work")):
            assert await composition.publish_generated(admitted)

    asyncio.run(with_database(scenario))


def test_fresh_post_work_consent_denial_prevents_any_publication():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        state["firestore_denied"] = True
        with pytest.raises(StandardTalkDenied, match="current_consent_denied"):
            await composition.publish_generated(admitted)
        assert state["loader_calls"] == 3
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0

    asyncio.run(with_database(scenario))


@pytest.mark.parametrize("phase", ["issue", "claim", "publish"])
def test_revoke_while_waiting_for_owner_lock_is_observed_before_transition(phase):
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = None if phase == "issue" else await composition.issue_server_turn(state["turn"])
        admitted = (
            await composition.claim_for_synthesis(reference.reference, state["turn"]) if phase == "publish" else None
        )
        async with pool.acquire() as conn:
            async with conn.transaction():
                owner = authority_advisory_lock.AuthorityOwner.from_values(state["owner"], state["owner"])
                await authority_advisory_lock.acquire_authority_lock(conn, owner=owner)
                if phase == "issue":
                    task = asyncio.create_task(composition.issue_server_turn(state["turn"]))
                elif phase == "claim":
                    task = asyncio.create_task(composition.claim_for_synthesis(reference.reference, state["turn"]))
                else:
                    task = asyncio.create_task(composition.publish_generated(admitted))
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    if await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=$1 AND wait_event='advisory')",
                        state["schema"],
                    ):
                        break
                else:
                    raise AssertionError("actual owner advisory lock wait required")
                assert not task.done()
                await conn.execute("UPDATE ella_managed_cloud_consent_authority SET decision='revoked'")
        with pytest.raises(StandardTalkDenied):
            await task
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0

    asyncio.run(with_database(scenario))


def test_synced_regrant_epoch_aba_cannot_publish_original_claim():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        epoch = uuid.uuid4()
        await pool.execute(
            "UPDATE ella_managed_cloud_consent_authority SET authority_epoch=$1, revision=revision+1", epoch
        )
        await pool.execute(
            "UPDATE voice_entitlements SET consent_authority_epoch=$1, consent_authority_revision=consent_authority_revision+1",
            epoch,
        )
        assert not await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0

    asyncio.run(with_database(scenario))


def test_synced_mirror_revision_change_with_same_epoch_cannot_adopt_original_claim():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        await pool.execute("UPDATE ella_managed_cloud_consent_authority SET revision=revision+1")
        await pool.execute("UPDATE voice_entitlements SET consent_authority_revision=consent_authority_revision+1")
        assert not await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0

    asyncio.run(with_database(scenario))


def test_legacy_generated_helper_still_upserts_and_started_makes_candidate():
    async def scenario(pool, state):
        await ledger.record_generated(pool, uid="synthetic", playback_id="legacy", playback_text="original")
        await ledger.record_generated(pool, uid="synthetic", playback_id="legacy", playback_text="replacement")
        assert await pool.fetchval("SELECT playback_text FROM guardian_playback_ledger") == "original"
        assert await ledger.get_played_candidates(pool, "synthetic") == []
        await ledger.record_playback_receipt(pool, uid="synthetic", playback_id="legacy", event_type="started")
        assert len(await ledger.get_played_candidates(pool, "synthetic")) == 1

    asyncio.run(with_database(scenario))


def test_missing_mirror_denies_without_bootstrap_even_with_stale_snapshot():
    async def scenario(pool, state):
        composition = await service(pool, state)
        fresh = await composition._fresh_authority("synthetic")
        await pool.execute("DELETE FROM ella_managed_cloud_consent_authority")

        async def stale_loader(uid):
            return fresh

        composition._fresh_authority = stale_loader
        with pytest.raises(StandardTalkDenied, match="authority_mirror_missing_or_stale"):
            await composition.issue_server_turn(state["turn"])
        assert await pool.fetchval("SELECT count(*) FROM ella_managed_cloud_consent_authority") == 0
        assert await pool.fetchval("SELECT count(*) FROM ella_standard_talk_playback_references") == 0

    asyncio.run(with_database(scenario))


def test_snapshot_binding_change_before_lock_is_rechecked_from_actual_row():
    async def scenario(pool, state):
        composition = await service(pool, state)
        original = composition._fresh_authority

        async def stale_loader(uid):
            fresh = await original(uid)
            await pool.execute("UPDATE ella_runtime_bindings SET revision=revision+1")
            return fresh

        composition._fresh_authority = stale_loader
        with pytest.raises(StandardTalkDenied, match="runtime_authority_changed"):
            await composition.issue_server_turn(state["turn"])
        assert await pool.fetchval("SELECT count(*) FROM ella_standard_talk_playback_references") == 0

    asyncio.run(with_database(scenario))


def test_changed_server_normalization_after_claim_cannot_publish():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])

        def changed(turn, raw):
            return VerifiedSpokenText("different", turn.canonical_text_sha256, hashlib.sha256(b"different").hexdigest())

        composition._verified_spoken_text = changed
        with pytest.raises(StandardTalkDenied, match="normalization_changed"):
            await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 0

    asyncio.run(with_database(scenario))


def test_preexisting_playback_id_denies_and_rolls_back_protected_publication():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        await ledger.record_generated(
            pool, uid="synthetic", playback_id=admitted.claim.playback_id, playback_text="existing"
        )
        with pytest.raises(ValueError, match="playback_ledger_candidate_conflict"):
            await composition.publish_generated(admitted)
        assert await pool.fetchval("SELECT state FROM ella_standard_talk_playback_references") == "synthesizing"
        assert await pool.fetchval("SELECT playback_text FROM guardian_playback_ledger") == "existing"
        assert await pool.fetchval("SELECT count(*) FROM guardian_playback_ledger") == 1

    asyncio.run(with_database(scenario))


def test_actual_two_connection_ledger_insert_race_rolls_back_protected_publication():
    async def scenario(pool, state):
        composition = await service(pool, state)
        reference = await composition.issue_server_turn(state["turn"])
        admitted = await composition.claim_for_synthesis(reference.reference, state["turn"])
        async with pool.acquire() as conn:
            async with conn.transaction():
                await ledger.record_generated(
                    pool,
                    uid="synthetic",
                    playback_id=admitted.claim.playback_id,
                    playback_text="racing-existing",
                    connection=conn,
                )
                task = asyncio.create_task(composition.publish_generated(admitted))
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    await conn.execute("SELECT pg_stat_clear_snapshot()")
                    if await conn.fetchval(
                        "SELECT EXISTS(SELECT 1 FROM pg_stat_activity WHERE application_name=$1 AND wait_event_type='Lock' AND query LIKE '%INSERT INTO guardian_playback_ledger%')",
                        state["schema"],
                    ):
                        break
                else:
                    raise AssertionError("actual unique-index insert wait required")
                assert not task.done()
                assert await conn.fetchval("SELECT state FROM ella_standard_talk_playback_references") == "synthesizing"
        with pytest.raises(ValueError, match="playback_ledger_candidate_conflict"):
            await task
        assert await pool.fetchval("SELECT state FROM ella_standard_talk_playback_references") == "synthesizing"
        assert await pool.fetchval("SELECT playback_text FROM guardian_playback_ledger") == "racing-existing"

    asyncio.run(with_database(scenario))
