import asyncio
import hashlib
from dataclasses import replace
from types import SimpleNamespace

import pytest

from database.standard_talk_playback_authority import ServerAssistantTurn, StandardTalkDenied
from database.standard_talk_playback_references import NORMALIZATION_VERSION
from ella.services.standard_talk_playback import FreshAuthority, StandardTalkPlaybackComposition, VerifiedSpokenText


def turn():
    return ServerAssistantTurn("synthetic", "turn", "session", hashlib.sha256(b"hello").hexdigest())


@pytest.mark.parametrize("configuration", [{}, {"enabled": True}, {"enabled": True, "fresh_authority": object()}])
def test_dormant_or_missing_trusted_dependency_denies_without_database(configuration):
    service = StandardTalkPlaybackComposition(None, **configuration)
    with pytest.raises(StandardTalkDenied, match="standard_talk_unavailable"):
        asyncio.run(service.issue_server_turn(turn()))


@pytest.mark.parametrize("field", ["uid", "provider", "runtime_target_mode", "profile_user_id"])
def test_wrong_owner_or_unsupported_lane_denies_before_database(field):
    runtime = SimpleNamespace(
        uid="synthetic",
        provider="hermes",
        runtime_target_mode="hermes-chat",
        account_user_id="same",
        profile_user_id="same",
    )
    setattr(runtime, field, "other")

    async def loader(uid):
        return FreshAuthority(runtime, SimpleNamespace(account_uid=uid, profile_uid=uid))

    service = StandardTalkPlaybackComposition(
        None, fresh_authority=loader, verified_spoken_text=lambda *_: None, enabled=True
    )
    with pytest.raises(StandardTalkDenied):
        asyncio.run(service.issue_server_turn(turn()))


@pytest.mark.parametrize(
    "field,value",
    [
        ("text", ""),
        ("canonical_text_sha256", "a" * 64),
        ("spoken_text_sha256", "b" * 64),
        ("normalization_version", "unreviewed"),
    ],
    ids=["empty", "canonical-digest", "spoken-digest", "version"],
)
def test_normalization_requires_explicit_version_and_exact_digests(field, value):
    current = turn()
    verified = VerifiedSpokenText("hello", current.canonical_text_sha256, hashlib.sha256(b"hello").hexdigest())
    service = StandardTalkPlaybackComposition(None, verified_spoken_text=lambda *_: replace(verified, **{field: value}))
    with pytest.raises(StandardTalkDenied, match="normalization_unavailable"):
        service._spoken(current, "hello")


def test_arbitrary_text_is_not_a_verified_server_normalization():
    service = StandardTalkPlaybackComposition(None, verified_spoken_text=lambda *_: "hello")
    with pytest.raises(StandardTalkDenied, match="normalization_unavailable"):
        service._spoken(turn(), "hello")


def test_verified_normalization_is_not_an_implemented_normalizer_or_public_api():
    current = turn()
    verified = VerifiedSpokenText("hello", current.canonical_text_sha256, hashlib.sha256(b"hello").hexdigest())
    service = StandardTalkPlaybackComposition(None, verified_spoken_text=lambda *_: verified)
    assert service._spoken(current, "hello") == "hello"
    assert verified.normalization_version == NORMALIZATION_VERSION
    assert "hello" not in repr(verified)


@pytest.mark.parametrize(
    "field,value", [("uid", ""), ("turn_id", ""), ("session_id", ""), ("canonical_text_sha256", "invalid")]
)
def test_invalid_canonical_coordinates_are_content_free_denials(field, value):
    with pytest.raises(StandardTalkDenied, match="canonical_turn_invalid"):
        replace(turn(), **{field: value})


@pytest.mark.parametrize("text", ["x" * 501, "\U0001f600" * 251], ids=["bmp-overflow", "astral-overflow"])
def test_spoken_bound_is_utf16_units_not_python_codepoints(text):
    verified = VerifiedSpokenText(text, turn().canonical_text_sha256, hashlib.sha256(text.encode()).hexdigest())
    service = StandardTalkPlaybackComposition(None, verified_spoken_text=lambda *_: verified)
    with pytest.raises(StandardTalkDenied, match="normalization_unavailable"):
        service._spoken(turn(), "hello")


def test_invalid_unicode_is_a_content_free_normalization_denial():
    verified = VerifiedSpokenText("\ud800", turn().canonical_text_sha256, "a" * 64)
    service = StandardTalkPlaybackComposition(None, verified_spoken_text=lambda *_: verified)
    with pytest.raises(StandardTalkDenied, match="normalization_unavailable"):
        service._spoken(turn(), "hello")
