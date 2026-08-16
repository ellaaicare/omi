import asyncio
import re
from datetime import datetime, timezone

import pytest

from ella.routers import chat
from ella.services.hermes_session import (
    canonical_omi_session_key,
    canonical_omi_session_key_migration,
    canonical_owner_component,
    preflight_canonical_omi_session_key_migration,
)


def test_temporal_chat_context_filters_morning_omi_fragments(monkeypatch):
    async def fake_fetch(uid, *, limit, before=None, channels=None, since=None, user_timezone=None):
        assert uid == "uid-1"
        assert channels == ["omi"]
        assert since
        return [
            {
                "event_id": "cafe",
                "channel": "omi",
                "title": "Cafe Visit - Ordering Food and Drinks",
                "text": (
                    "A full morning cafe visit with food and drink orders, including several specific items, "
                    "a longer exchange about the cafe, and enough surrounding detail to count as a meaningful "
                    "conversation rather than a one-word fragment."
                ),
                "started_at": "2026-05-11T17:49:30Z",
                "ended_at": "2026-05-11T18:05:00Z",
            },
            {
                "event_id": "brief",
                "channel": "omi",
                "title": "Brief Utterance",
                "text": "Okay.",
                "started_at": "2026-05-11T18:56:15Z",
                "ended_at": "2026-05-11T18:56:16Z",
                "metadata": {"ella_tags": ["omi", "low_signal"]},
            },
        ]

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return (
                datetime(2026, 5, 11, 20, 0, tzinfo=timezone.utc).astimezone(tz) if tz else datetime(2026, 5, 11, 20, 0)
            )

    monkeypatch.setattr(chat, "_fetch_chat_canonical_events", fake_fetch)
    monkeypatch.setattr(chat, "datetime", FixedDateTime)

    label, events = asyncio.run(chat._fetch_temporal_chat_context("uid-1", "what happened this morning?"))

    assert label == "same-day morning OMI context"
    assert [event["event_id"] for event in events] == ["cafe"]


def test_ios_chat_event_uses_stable_turn_identity():
    started_at = datetime(2026, 5, 27, 18, 30, tzinfo=timezone.utc)

    event = chat._ios_chat_event(
        uid="uid-1",
        turn_id="client-123",
        role="user",
        text="Remember the demo banana is on the blue shelf.",
        session_key="ella:omi:uid-1:canonical",
        started_at=started_at,
        client_info={"type": "ios-app"},
    )
    normalized = event.normalized()

    assert event.channel == "ios_chat"
    assert event.provider == "omi-ios-chat"
    assert event.role == "user"
    assert event.scan_policy == "immediate"
    assert event.event_id == "ios_chat:uid-1:client-123:user"
    assert normalized["source_identity"] == "ios_chat:uid-1:client-123"
    assert event.session_id == "ella:omi:uid-1:canonical"


def test_ios_chat_assistant_event_disables_scan_policy():
    started_at = datetime(2026, 5, 27, 18, 31, tzinfo=timezone.utc)

    event = chat._ios_chat_event(
        uid="uid-1",
        turn_id="client-123",
        role="assistant",
        text="I will remember that.",
        session_key="ella:omi:uid-1:canonical",
        started_at=started_at,
    )

    assert event.scan_policy == "none"
    assert event.event_id == "ios_chat:uid-1:client-123:assistant"


def test_hermes_session_defaults_to_canonical(monkeypatch):
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")

    assert chat._hermes_chat_session_key("ABC123") == canonical_omi_session_key("ABC123")
    assert chat._hermes_chat_session_key("User/123") == canonical_omi_session_key("User/123")
    assert chat._hermes_chat_memory_key("User/123") == canonical_omi_session_key("User/123")


def test_canonical_session_keys_preserve_exact_collision_safe_owner_identity(monkeypatch):
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "canonical")
    owner_vectors = ["CaseUID", "caseuid", "Case/UID", "Case-2FUID", "Case UID", "Case%2FUID"]
    keys = [canonical_omi_session_key(uid) for uid in owner_vectors]
    downstream_keys = [re.sub(r"[^a-zA-Z0-9_-]+", "-", key) for key in keys]

    assert len(set(keys)) == len(owner_vectors)
    assert len(set(downstream_keys)) == len(owner_vectors)
    assert keys == [canonical_omi_session_key(uid) for uid in owner_vectors]
    assert all(re.fullmatch(r"ella:omi:v2-[0-9a-f]{64}:canonical", key) for key in keys)
    assert all(len(key.encode("ascii")) < 256 for key in keys)
    assert all(len(key.encode("ascii")) <= 100 for key in downstream_keys)
    assert all(uid not in key for uid, key in zip(owner_vectors, keys))
    assert chat._hermes_chat_session_key("CaseUID") == canonical_omi_session_key("CaseUID")


def test_exact_owner_keys_never_reuse_the_case_folded_legacy_memory_scope():
    legacy_case_folded_key = "ella:omi:caseuid:canonical"
    retained_owner = canonical_omi_session_key_migration("CaseUID")
    case_colliding_owner = canonical_omi_session_key_migration("caseuid")

    assert retained_owner.legacy_key == case_colliding_owner.legacy_key == legacy_case_folded_key
    assert retained_owner.v2_key != legacy_case_folded_key
    assert case_colliding_owner.v2_key != legacy_case_folded_key
    assert retained_owner.v2_key != case_colliding_owner.v2_key


@pytest.mark.parametrize(
    ("uid", "legacy_bytes", "v2_bytes"),
    [
        (
            "Firebase-User-A",
            b"ella:omi:firebase-user-a:canonical",
            b"ella:omi:v2-cb9ad1acbe46f34ce38725add7f338cf4ff69d6265454ec830f5d76167be7b54:canonical",
        ),
        (
            "UID With Spaces / Symbols",
            b"ella:omi:uid-with-spaces-symbols:canonical",
            b"ella:omi:v2-16daead6f88d69e54d5b85180ab1252d9ee2222ca87dd46ec1fa6f3a98b709e8:canonical",
        ),
        (
            "Case%2FUID",
            b"ella:omi:case-2fuid:canonical",
            b"ella:omi:v2-b05d996c8d56734a29d50c2dc40e2f9f1c3153a19169cad4a73d0cced65009ab:canonical",
        ),
        (
            "Case/UID",
            b"ella:omi:case-uid:canonical",
            b"ella:omi:v2-2edb8df08622e5ef614a88867ffff12c038077dd11bccead07a5be0476b44ad9:canonical",
        ),
        (
            "Case-2FUID",
            b"ella:omi:case-2fuid:canonical",
            b"ella:omi:v2-d669f18291ad0f633baa2040c2471d9deba8580266a159e1c17686d32849a55b:canonical",
        ),
        (
            "---",
            b"ella:omi:unknown:canonical",
            b"ella:omi:v2-1f677c0e9c201515481f25629c06932f0d8af584b734dd637b945eb91ca52592:canonical",
        ),
        (
            "A" * 200,
            b"ella:omi:" + (b"a" * 160) + b":canonical",
            b"ella:omi:v2-1d1978c22a983b683e694a712d06d16471ce0a560ad63dd58375a418096bf4e4:canonical",
        ),
    ],
)
def test_legacy_to_v2_cross_repo_session_contract_is_byte_exact(uid, legacy_bytes, v2_bytes):
    migration = canonical_omi_session_key_migration(uid)

    assert migration.legacy_key.encode("utf-8") == legacy_bytes
    assert migration.v2_key.encode("utf-8") == v2_bytes
    assert migration.v2_key == canonical_omi_session_key(uid)
    assert migration.legacy_key != migration.v2_key
    assert uid not in migration.v2_key


def test_migration_preflight_requires_v2_target_disjoint_from_every_retained_legacy_key():
    retained_legacy_keys = {
        canonical_omi_session_key_migration(uid).legacy_key
        for uid in ("CaseUID", "caseuid", "Case/UID", "Case-2FUID", "Case%2FUID")
    }
    migration = preflight_canonical_omi_session_key_migration("CaseUID", retained_legacy_keys)

    assert set(vars(migration)) == {"legacy_key", "v2_key"}
    assert migration.v2_key not in retained_legacy_keys
    with pytest.raises(ValueError, match="collides with a retained legacy key"):
        preflight_canonical_omi_session_key_migration(
            "CaseUID",
            retained_legacy_keys | {migration.v2_key},
        )


def test_migration_preflight_rejects_malformed_retained_key_inventory():
    with pytest.raises(ValueError, match="iterable of non-empty strings"):
        preflight_canonical_omi_session_key_migration("CaseUID", [""])
    with pytest.raises(ValueError, match="iterable of non-empty strings"):
        preflight_canonical_omi_session_key_migration("CaseUID", "legacy-key")


@pytest.mark.parametrize("uid", ["", " ", " CaseUID", "CaseUID ", None, 123])
def test_canonical_owner_component_rejects_empty_or_non_string_uid(uid):
    with pytest.raises(ValueError, match="non-empty string without surrounding whitespace"):
        canonical_owner_component(uid)


def test_direct_chat_session_uses_the_same_exact_encoded_owner_component(monkeypatch):
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_SCOPE", "daily")
    monkeypatch.setattr(chat, "HERMES_CHAT_SESSION_EPOCH", "epoch-1")

    case_component = canonical_owner_component("CaseUID")
    unsafe_component = canonical_owner_component("Case/User %")

    assert chat._hermes_chat_session_key("CaseUID") == f"ella:omi:{case_component}:ios-chat:epoch-1"
    assert chat._hermes_chat_session_key("CaseUID") != chat._hermes_chat_session_key("caseuid")
    assert chat._hermes_chat_session_key("Case/User %") == f"ella:omi:{unsafe_component}:ios-chat:epoch-1"


def test_hermes_chat_headers_include_stable_session_key():
    component = canonical_owner_component("abc123")
    session_id = f"ella:omi:{component}:ios-chat:daily-20260530"
    session_key = canonical_omi_session_key("abc123")
    headers = chat._hermes_chat_headers(session_id, session_key)

    assert headers["X-Hermes-Session-Id"] == session_id
    assert headers["X-Hermes-Session-Key"] == session_key
    assert headers["Content-Type"] == "application/json"
