import asyncio
import time

from utils.ella import scanner_keyterms


SCANNER_TUNING = """
## @runtime: current-state
[ACTIVE]
- guardian_mode: EMERGENCY_ONLY

## @wakeword: defaults
[ACTIVE]
- Hey Ella
- Ella
- "Hey Dina"

## @wakeword: personal-learned
[ACTIVE]
- where did I put my glasses → response_template: item_location_lookup[glasses]
- keys location lookup | response_template: item_location_lookup[keys]

## @prefilter: media-baseline
[ACTIVE]
- suppress: podcast audio
- ignore: "normal TV audio"

## @scanner: health-context
[ACTIVE]
- medications: metformin, rescue inhaler
- providers: Dr. Pu, Claudia
- conditions: type 2 diabetes, hypertension

## @fastpath: old-temp-rule
[ACTIVE]
- expires: 2020-01-01
- obsolete phrase

## @wakeword: inactive-test
[INACTIVE]
- Should Not Appear
"""


def setup_function():
    scanner_keyterms.clear_scanner_keyterm_cache()


def test_parse_scanner_tuning_keyterms_prioritizes_active_wake_and_learned_terms():
    terms = scanner_keyterms.parse_scanner_tuning_keyterms(SCANNER_TUNING)

    assert terms[:5] == [
        "Hey Ella",
        "Ella",
        "Hey Dina",
        "where did I put my glasses",
        "keys location lookup",
    ]
    assert "metformin" in terms
    assert "Dr. Pu" in terms
    assert "type 2 diabetes" in terms
    assert "normal TV audio" not in terms
    assert "podcast audio" not in terms
    assert "obsolete phrase" not in terms
    assert "Should Not Appear" not in terms


def test_combine_deepgram_keyterms_gives_scanner_terms_priority():
    vocabulary = ["Omi", "generic", "Hey Ella", *[f"generic-{i}" for i in range(100)]]
    scanner_terms = ["Hey Ella", "metformin", "Dr. Pu"]

    combined = scanner_keyterms.combine_deepgram_keyterms(vocabulary, scanner_terms)

    assert combined[:3] == ["Hey Ella", "metformin", "Dr. Pu"]
    assert len(combined) == scanner_keyterms.DEFAULT_DEEPGRAM_MAX_TERMS
    assert combined.count("Hey Ella") == 1


def test_combine_deepgram_keyterms_uses_provider_specific_budget(monkeypatch):
    monkeypatch.setenv("ELLA_DEEPGRAM_KEYTERMS_MAX_TERMS", "3")

    combined = scanner_keyterms.combine_deepgram_keyterms(
        ["generic"],
        ["Hey Ella", "where did I put my glasses", "Dr. Pu", "metformin"],
    )

    assert combined == ["Hey Ella", "where did I put my glasses", "Dr. Pu"]


def test_limit_keyterms_enforces_token_budget():
    terms = [
        "one two three",
        "four five six",
        "seven eight nine",
    ]

    assert scanner_keyterms.limit_keyterms(terms, max_terms=100, max_tokens=6) == [
        "one two three",
        "four five six",
    ]


def test_get_scanner_keyterms_returns_cached_terms_without_refresh(monkeypatch):
    scanner_keyterms._cache["agent-1"] = scanner_keyterms.KeytermCacheEntry(
        terms=["Hey Ella"],
        agent_id="agent-1",
        fetched_at=time.time(),
        source="test",
    )
    scanner_keyterms._uid_agent_ids["uid-1"] = "agent-1"

    async def fail_refresh(*args, **kwargs):
        raise AssertionError("fresh cache should not refresh")

    monkeypatch.setattr(scanner_keyterms, "refresh_scanner_keyterms", fail_refresh)

    assert asyncio.run(scanner_keyterms.get_scanner_keyterms("uid-1")) == ["Hey Ella"]


def test_get_scanner_keyterms_cache_miss_returns_empty_and_schedules_refresh(monkeypatch):
    scheduled = {}

    async def fake_refresh(uid, agent_id=None):
        scheduled["uid"] = uid
        return ["Hey Ella"]

    class FakeLoop:
        def create_task(self, coro):
            coro.close()
            scheduled["created"] = True

            class FakeTask:
                def add_done_callback(self, callback):
                    return None

            return FakeTask()

    monkeypatch.setattr(scanner_keyterms.asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(scanner_keyterms, "refresh_scanner_keyterms", fake_refresh)

    assert asyncio.run(scanner_keyterms.get_scanner_keyterms("uid-1")) == []
    assert scheduled["created"] is True


def test_refresh_scanner_keyterms_fetches_provision_file(monkeypatch):
    requests = []

    async def fake_resolve(uid):
        return "agent-1"

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"content": SCANNER_TUNING}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            requests.append((url, headers, self.timeout))
            return FakeResponse()

    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://provision")
    monkeypatch.setenv("ELLA_PROVISION_API_TOKEN", "token-1")
    monkeypatch.setattr(scanner_keyterms, "_resolve_agent_id", fake_resolve)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", FakeClient)

    terms = asyncio.run(scanner_keyterms.refresh_scanner_keyterms("uid-1"))

    assert "Hey Ella" in terms
    assert "metformin" in terms
    assert requests == [
        (
            "http://provision/workspace/agent-1/files/scanner-tuning.md",
            {"Authorization": "Bearer token-1"},
            scanner_keyterms.DEFAULT_TIMEOUT_SECONDS,
        )
    ]
    assert scanner_keyterms.cache_status("uid-1")["count"] == len(terms)


def test_fetch_scanner_tuning_does_not_use_shared_fallback_by_default(monkeypatch):
    requests = []

    class FakeResponse:
        status_code = 404
        text = "not found"

        def json(self):
            return {"error": "not found"}

        def raise_for_status(self):
            raise scanner_keyterms.httpx.HTTPStatusError(
                "not found",
                request=scanner_keyterms.httpx.Request("GET", requests[-1]),
                response=scanner_keyterms.httpx.Response(404),
            )

    class FakeClient:
        def __init__(self, timeout):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            requests.append(url)
            return FakeResponse()

    monkeypatch.setenv("ELLA_PROVISION_API_URL", "http://provision")
    monkeypatch.delenv("ELLA_SCANNER_KEYTERMS_ALLOW_SHARED_FALLBACK", raising=False)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", FakeClient)

    try:
        asyncio.run(scanner_keyterms._fetch_scanner_tuning("agent-1"))
    except scanner_keyterms.httpx.HTTPStatusError:
        pass

    assert requests == ["http://provision/workspace/agent-1/files/scanner-tuning.md"]
