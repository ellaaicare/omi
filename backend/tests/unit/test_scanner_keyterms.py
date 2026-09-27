import asyncio
import time
from types import SimpleNamespace

import pytest

from ella.utils import provision_authority
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


def _configure_hermes_provision_authority(monkeypatch):
    url = provision_authority.APPROVED_HERMES_PROVISION_URL
    token = "synthetic-hermes-keyterm-token"
    binding_name = "ELLA_HERMES_PROVISION_KEYTERM_BINDING"
    monkeypatch.setenv(provision_authority.HERMES_PROVISION_URL_ENV, url)
    monkeypatch.setenv(provision_authority.HERMES_PROVISION_TOKEN_ENV, token)
    monkeypatch.setenv(provision_authority.HERMES_PROVISION_ALLOWLIST_ENV, url)
    monkeypatch.setenv(provision_authority.HERMES_PROVISION_BINDING_REF_ENV, f"env:{binding_name}")
    monkeypatch.setenv(binding_name, provision_authority._authority_binding_value(url, token))
    monkeypatch.setenv(provision_authority.LEGACY_PROVISION_URL_ENV, provision_authority.DEFAULT_LEGACY_PROVISION_URL)
    monkeypatch.setenv(provision_authority.LEGACY_PROVISION_TOKEN_ENV, "synthetic-distinct-legacy-token")


@pytest.fixture(autouse=True)
def retained_runtime_not_invitation_owned(monkeypatch):
    async def authority_disabled(_uid):
        return False

    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_disabled)


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


def test_isolated_scanner_uses_hermes_workspace_and_drops_legacy_cache(monkeypatch):
    requests = []

    async def authority_enabled(_uid):
        return True

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-isolated"
        assert target_mode == "hermes-cloud-guardian"
        return SimpleNamespace(agent_id="omi-isolated", provider="hermes")

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"content": SCANNER_TUNING}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, timeout, trust_env):
            self.timeout = timeout
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            requests.append((url, headers))
            return FakeResponse()

    scanner_keyterms._cache["uid-isolated"] = scanner_keyterms.KeytermCacheEntry(
        terms=["shared-term"],
        agent_id="legacy-agent",
        fetched_at=time.time(),
        source="provision_api",
    )
    scanner_keyterms._uid_agent_ids["uid-isolated"] = "legacy-agent"
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED", "false")
    monkeypatch.setenv("ELLA_RUNTIME_BINDINGS_ENABLED_UIDS", "uid-isolated")
    _configure_hermes_provision_authority(monkeypatch)
    monkeypatch.setenv("ELLA_SCANNER_KEYTERMS_ALLOW_SHARED_FALLBACK", "true")
    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(scanner_keyterms, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", FakeClient)

    terms = asyncio.run(scanner_keyterms.refresh_scanner_keyterms("uid-isolated"))

    assert "Hey Ella" in terms
    assert "shared-term" not in terms
    assert requests == [
        (
            f"{provision_authority.APPROVED_HERMES_PROVISION_URL}/workspace/omi-isolated/files/scanner-tuning.md",
            {
                "Authorization": "Bearer synthetic-hermes-keyterm-token",
                "X-Ella-Owner-Uid": "uid-isolated",
            },
        )
    ]
    assert scanner_keyterms._cache["uid-isolated"].source == "isolated:hermes"


def test_isolated_scanner_ignores_environment_proxies(monkeypatch):
    client_options = {}

    async def authority_enabled(_uid):
        return True

    class FakeResponse:
        status_code = 200
        text = ""

        def json(self):
            return {"content": SCANNER_TUNING}

        def raise_for_status(self):
            return None

    class FakeClient:
        def __init__(self, **kwargs):
            client_options.update(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args, **_kwargs):
            return FakeResponse()

    _configure_hermes_provision_authority(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:8080")
    monkeypatch.setenv("ALL_PROXY", "socks5://proxy.invalid:1080")
    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", FakeClient)

    asyncio.run(scanner_keyterms._fetch_scanner_tuning("omi-isolated", uid="uid-isolated"))

    assert client_options == {
        "timeout": scanner_keyterms.DEFAULT_TIMEOUT_SECONDS,
        "trust_env": False,
    }


def test_isolated_scanner_fails_before_request_without_bound_authority(monkeypatch):
    async def authority_enabled(_uid):
        return True

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("Missing isolated authority must fail before network egress")

    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.delenv(provision_authority.HERMES_PROVISION_URL_ENV, raising=False)
    monkeypatch.delenv(provision_authority.HERMES_PROVISION_TOKEN_ENV, raising=False)
    monkeypatch.delenv(provision_authority.HERMES_PROVISION_BINDING_REF_ENV, raising=False)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", ForbiddenClient)

    with pytest.raises(scanner_keyterms.ProvisioningError) as raised:
        asyncio.run(scanner_keyterms._fetch_scanner_tuning("omi-isolated", uid="uid-isolated"))

    assert raised.value.code == "hermes_provision_authority_incomplete"
    assert raised.value.retryable is True


@pytest.mark.parametrize(
    ("mutation", "expected_code"),
    [
        ("missing_url", "hermes_provision_authority_incomplete"),
        ("missing_binding", "hermes_provision_authority_incomplete"),
        ("malformed_binding", "hermes_provision_authority_binding_invalid"),
        ("binding_mismatch", "hermes_provision_authority_binding_invalid"),
        ("rejected_destination", "hermes_provision_authority_destination_rejected"),
        ("legacy_coordinate_conflict", "provision_authority_pair_conflict"),
    ],
)
def test_isolated_scanner_rejects_incomplete_or_conflicting_authority_before_egress(
    monkeypatch,
    mutation,
    expected_code,
):
    async def authority_enabled(_uid):
        return True

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("Invalid isolated authority must fail before network egress")

    _configure_hermes_provision_authority(monkeypatch)
    if mutation == "missing_url":
        monkeypatch.delenv(provision_authority.HERMES_PROVISION_URL_ENV)
    elif mutation == "missing_binding":
        monkeypatch.delenv(provision_authority.HERMES_PROVISION_BINDING_REF_ENV)
    elif mutation == "malformed_binding":
        monkeypatch.setenv(provision_authority.HERMES_PROVISION_BINDING_REF_ENV, "literal-not-a-secret-ref")
    elif mutation == "binding_mismatch":
        monkeypatch.setenv("ELLA_HERMES_PROVISION_KEYTERM_BINDING", "sha256:" + "0" * 64)
    elif mutation == "rejected_destination":
        monkeypatch.setenv(provision_authority.HERMES_PROVISION_URL_ENV, "http://127.0.0.1:8210")
    elif mutation == "legacy_coordinate_conflict":
        monkeypatch.setenv(
            provision_authority.LEGACY_PROVISION_URL_ENV,
            provision_authority.APPROVED_HERMES_PROVISION_URL,
        )

    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", ForbiddenClient)

    with pytest.raises(scanner_keyterms.ProvisioningError) as raised:
        asyncio.run(scanner_keyterms._fetch_scanner_tuning("omi-isolated", uid="uid-isolated"))

    assert raised.value.code == expected_code
    assert raised.value.retryable is True


def test_isolated_scanner_revalidates_authority_snapshot_immediately_before_egress(monkeypatch):
    async def authority_enabled(_uid):
        return True

    class ForbiddenClient:
        def __init__(self, timeout, trust_env):
            self.timeout = timeout
            assert trust_env is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, *_args, **_kwargs):
            raise AssertionError("Authority drift must fail before network egress")

    _configure_hermes_provision_authority(monkeypatch)
    actual_authority = scanner_keyterms.hermes_provision_authority
    calls = 0

    def drifting_authority(expected_snapshot=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            drifted_token = "synthetic-drifted-hermes-keyterm-token"
            monkeypatch.setenv(
                provision_authority.HERMES_PROVISION_TOKEN_ENV,
                drifted_token,
            )
            monkeypatch.setenv(
                "ELLA_HERMES_PROVISION_KEYTERM_BINDING",
                provision_authority._authority_binding_value(
                    provision_authority.APPROVED_HERMES_PROVISION_URL,
                    drifted_token,
                ),
            )
        return actual_authority(expected_snapshot)

    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(scanner_keyterms, "hermes_provision_authority", drifting_authority)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", ForbiddenClient)

    with pytest.raises(scanner_keyterms.ProvisioningError) as raised:
        asyncio.run(scanner_keyterms._fetch_scanner_tuning("omi-isolated", uid="uid-isolated"))

    assert raised.value.code == "hermes_provision_authority_drift"
    assert calls == 3


def test_cloud_scanner_never_calls_mini_or_returns_retained_cache(monkeypatch):
    async def authority_enabled(uid=None):
        return uid == "uid-cloud"

    async def fake_runtime(uid, *, target_mode=None):
        assert uid == "uid-cloud"
        assert target_mode == "hermes-cloud-guardian"
        return SimpleNamespace(agent_id="cloud-agent", provider="hermes_cloud")

    class ForbiddenClient:
        def __init__(self, **_kwargs):
            raise AssertionError("Cloud scanner must not call a Mini workspace endpoint")

    scanner_keyterms._cache["uid-cloud"] = scanner_keyterms.KeytermCacheEntry(
        terms=["retained-plato-term"],
        agent_id="legacy-agent",
        fetched_at=time.time(),
        source="isolated:hermes",
    )
    monkeypatch.setattr(scanner_keyterms, "runtime_authority_enabled", authority_enabled)
    monkeypatch.setattr(scanner_keyterms, "resolve_isolated_runtime", fake_runtime)
    monkeypatch.setattr(scanner_keyterms.httpx, "AsyncClient", ForbiddenClient)

    terms = asyncio.run(scanner_keyterms.refresh_scanner_keyterms("uid-cloud"))

    assert terms == []
    assert "uid-cloud" not in scanner_keyterms._cache


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
