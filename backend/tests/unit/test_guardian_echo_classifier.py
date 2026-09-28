import asyncio
import json

import httpx
import pytest

from ella.services import guardian_echo_classifier as classifier


def _candidates():
    return [
        {
            "playback_id": "guardian_abc123",
            "text": "Hi Greg, I heard my name. I'm here with you.",
            "started_at": "2026-09-28T15:04:05+00:00",
            "completed_at": "2026-09-28T15:04:07.5+00:00",
            "duration_ms": 2500,
        }
    ]


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, text="ok"):
        self.status_code = status_code
        self._json_body = json_body if json_body is not None else {}
        self.text = text

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    """Records posted requests and returns queued canned responses in order."""

    queued_responses = []
    posts = []
    raise_on_post = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        _FakeAsyncClient.posts.append((url, kwargs))
        if _FakeAsyncClient.raise_on_post is not None:
            raise _FakeAsyncClient.raise_on_post
        return _FakeAsyncClient.queued_responses.pop(0)


@pytest.fixture(autouse=True)
def _reset_fake_client(monkeypatch):
    _FakeAsyncClient.queued_responses = []
    _FakeAsyncClient.posts = []
    _FakeAsyncClient.raise_on_post = None
    monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient)
    # Default: only the Jev key configured, matching most fixtures below.
    monkeypatch.setattr(classifier, "_JEV_API_KEY", "test-jev-key")
    monkeypatch.setattr(classifier, "_FALLBACK_API_KEY", "")
    yield


def _decision_response(**overrides):
    body = {
        "schema_version": "guardian_playback_source_v1",
        "is_ella_playback": False,
        "source": "unclear",
        "contains_additional_live_speech": True,
        "matched_playback_ids": [],
        "live_speech_spans": [],
        "confidence": 0.0,
        "reason_code": "test_fixture",
    }
    body.update(overrides)
    return _FakeResponse(json_body={"decision": body})


# --- Semantic fixtures (mocked provider) -----------------------------------


def test_exact_echo_is_confirmed():
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="ella_playback",
            contains_additional_live_speech=False,
            matched_playback_ids=["guardian_abc123"],
            confidence=0.98,
            reason_code="exact_echo",
        )
    ]

    result = asyncio.run(classify_playback_source_test("Hi Greg, I heard my name. I'm here with you."))

    assert result.is_confirmed_echo
    assert not result.fail_open
    assert result.matched_playback_ids == ["guardian_abc123"]


def test_paraphrased_echo_is_confirmed():
    transcript = "It said hi Greg and something about hearing my name"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="ella_playback",
            contains_additional_live_speech=False,
            matched_playback_ids=["guardian_abc123"],
            confidence=0.81,
            reason_code="paraphrased_echo",
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.is_confirmed_echo


def test_partial_misheard_echo_is_mixed_with_validated_live_span():
    transcript = "Hi Greg I heard my name what did you just say about dinner"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="mixed",
            contains_additional_live_speech=True,
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=["what did you just say about dinner"],
            confidence=0.7,
            reason_code="partial_echo_with_live_followup",
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.is_confirmed_echo
    assert result.validated_live_spans == ["what did you just say about dinner"]


def test_tv_media_is_not_confirmed_echo():
    transcript = "Hi Greg, welcome back to the show, I'm here with you tonight"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=False,
            source="tv_media",
            contains_additional_live_speech=False,
            confidence=0.6,
            reason_code="tv_dialogue",
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert not result.is_confirmed_echo
    assert result.source == "tv_media"


def test_unrelated_user_speech_is_not_confirmed_echo():
    transcript = "Can you remind me to call my daughter tomorrow"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=False,
            source="live_user",
            contains_additional_live_speech=False,
            confidence=0.95,
            reason_code="unrelated_live_speech",
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert not result.is_confirmed_echo
    assert result.source == "live_user"


def test_user_talking_over_whisper_keeps_only_validated_live_span():
    transcript = "Hi Greg I heard my na I said stop talking over me"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="mixed",
            contains_additional_live_speech=True,
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=["I said stop talking over me"],
            confidence=0.65,
            reason_code="talked_over_playback",
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.source == "mixed"
    assert result.validated_live_spans == ["I said stop talking over me"]


def test_mixed_with_unvalidated_spans_fails_open_on_original_transcript():
    transcript = "Hi Greg I heard my name and then something else happened"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="mixed",
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=["a rewritten span that is not in the transcript"],
            confidence=0.5,
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_INVALID_SCHEMA
    assert result.validated_live_spans == []
    assert not result.is_confirmed_echo


def test_mixed_with_empty_live_speech_spans_list_fails_open_on_original_transcript():
    """mixed + a matched playback id but an empty span list must never suppress.

    Without at least one extractive span there is nothing to retain, so the
    contract requires this to come back unclear/fail-open rather than a
    confirmed echo that would drop the (claimed) live speech silently.
    """
    transcript = "Hi Greg I heard my name and then something else happened"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="mixed",
            contains_additional_live_speech=True,
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=[],
            confidence=0.5,
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.fail_open
    assert result.source == "unclear"
    assert result.reason_code == classifier.REASON_INVALID_SCHEMA
    assert result.validated_live_spans == []
    assert not result.is_confirmed_echo


def test_mixed_with_only_blank_live_speech_spans_fails_open_on_original_transcript():
    """A whitespace-only span is technically an extractive substring of most
    transcripts (they contain spaces) but carries no real speech — it must
    not satisfy the cross-field invariant either."""
    transcript = "Hi Greg I heard my name and then something else happened"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="mixed",
            contains_additional_live_speech=True,
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=["   ", ""],
            confidence=0.5,
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.fail_open
    assert result.source == "unclear"
    assert result.reason_code == classifier.REASON_INVALID_SCHEMA
    assert result.validated_live_spans == []
    assert not result.is_confirmed_echo


def test_contains_additional_live_speech_true_without_spans_fails_open_even_for_non_mixed_source():
    """The invariant is keyed on `contains_additional_live_speech`, not just
    `source == "mixed"` — a payload that declares extra live speech through
    the boolean flag alone must also be backed by a real span."""
    transcript = "Hi Greg I heard my name and then something else happened"
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="ella_playback",
            contains_additional_live_speech=True,
            matched_playback_ids=["guardian_abc123"],
            live_speech_spans=[],
            confidence=0.5,
        )
    ]

    result = asyncio.run(classify_playback_source_test(transcript))

    assert result.fail_open
    assert result.source == "unclear"
    assert result.reason_code == classifier.REASON_INVALID_SCHEMA
    assert not result.is_confirmed_echo


# --- Fail-open cases ---------------------------------------------------------


def test_no_provider_key_fails_open_without_any_call(monkeypatch):
    monkeypatch.setattr(classifier, "_JEV_API_KEY", "")
    monkeypatch.setattr(classifier, "_FALLBACK_API_KEY", "")

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_NO_KEY
    assert _FakeAsyncClient.posts == []


def test_no_candidates_fails_open_without_any_call():
    result = asyncio.run(classifier.classify_playback_source("Hi Greg I heard my name", [], uid="uid-1"))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_NO_CANDIDATES
    assert _FakeAsyncClient.posts == []


def test_timeout_fails_open():
    _FakeAsyncClient.raise_on_post = httpx.TimeoutException("timed out")

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_TIMEOUT
    assert not result.is_confirmed_echo


def test_provider_http_error_fails_open():
    _FakeAsyncClient.queued_responses = [_FakeResponse(status_code=500, text="boom")]

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_PROVIDER_ERROR


def test_invalid_schema_fails_open():
    _FakeAsyncClient.queued_responses = [_FakeResponse(json_body={"decision": {"nonsense": True}})]

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.fail_open
    assert result.reason_code == classifier.REASON_INVALID_SCHEMA


def test_explicit_unclear_is_fail_open_and_never_confirmed():
    _FakeAsyncClient.queued_responses = [_decision_response(source="unclear", confidence=0.3, reason_code="ambiguous")]

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.fail_open
    assert not result.is_confirmed_echo


def test_jev_failure_falls_back_to_generic_llm(monkeypatch):
    monkeypatch.setattr(classifier, "_FALLBACK_API_KEY", "test-fallback-key")
    _FakeAsyncClient.raise_on_post = None
    _FakeAsyncClient.queued_responses = [
        _FakeResponse(status_code=500, text="jev down"),
        _FakeResponse(
            json_body={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "schema_version": "guardian_playback_source_v1",
                                    "is_ella_playback": True,
                                    "source": "ella_playback",
                                    "contains_additional_live_speech": False,
                                    "matched_playback_ids": ["guardian_abc123"],
                                    "live_speech_spans": [],
                                    "confidence": 0.9,
                                    "reason_code": "fallback_exact_echo",
                                }
                            )
                        }
                    }
                ]
            }
        ),
    ]

    result = asyncio.run(classify_playback_source_test("Hi Greg I heard my name"))

    assert result.is_confirmed_echo
    assert len(_FakeAsyncClient.posts) == 2


# --- Provider prompt scoping / no raw text in logs --------------------------


def test_provider_prompt_contains_only_owner_candidates():
    _FakeAsyncClient.queued_responses = [_decision_response()]

    asyncio.run(classify_playback_source_test("Hi Greg I heard my name", uid="uid-owner"))

    _url, kwargs = _FakeAsyncClient.posts[0]
    sent_candidates = kwargs["json"]["input"]["playback_candidates"]
    assert sent_candidates == [
        {
            "playback_id": "guardian_abc123",
            "text": "Hi Greg, I heard my name. I'm here with you.",
            "started_at": "2026-09-28T15:04:05+00:00",
            "completed_at": "2026-09-28T15:04:07.5+00:00",
            "duration_ms": 2500,
        }
    ]
    assert set(kwargs["json"]["input"].keys()) == {"transcript", "playback_candidates"}


def test_no_raw_text_in_captured_logs(capsys):
    _FakeAsyncClient.queued_responses = [
        _decision_response(
            is_ella_playback=True,
            source="ella_playback",
            matched_playback_ids=["guardian_abc123"],
            confidence=0.9,
        )
    ]
    secret_transcript = "Hi Greg, super secret personal detail I heard my name"

    asyncio.run(classify_playback_source_test(secret_transcript, uid="uid-owner"))

    captured = capsys.readouterr()
    assert secret_transcript not in captured.out
    assert "super secret personal detail" not in captured.out
    assert "uid-owner" in captured.out


async def classify_playback_source_test(transcript: str, *, uid: str = "uid-1"):
    return await classifier.classify_playback_source(transcript, _candidates(), uid=uid)
