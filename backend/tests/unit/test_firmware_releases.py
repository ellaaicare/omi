import asyncio
import sys
import types
from urllib.parse import parse_qs, urlparse

redis_stub = types.ModuleType("database.redis_db")
redis_stub.get_generic_cache = lambda _key: None
redis_stub.set_generic_cache = lambda *_args, **_kwargs: None
sys.modules.setdefault("database.redis_db", redis_stub)

from routers import firmware


def _release(tag_name, published_at="2026-01-01T00:00:00Z", assets=None, body=""):
    return {
        "tag_name": tag_name,
        "published_at": published_at,
        "draft": False,
        "assets": assets or [],
        "body": body,
    }


def test_get_omi_github_releases_paginates_past_first_page(monkeypatch):
    first_page = [_release(f"v0.11.{idx}+macos") for idx in range(100)]
    second_page = [
        _release(
            "Omi_DK2_v2.0.10",
            assets=[
                {
                    "name": "Omi_DK2_OTA_v2.0.10.zip",
                    "browser_download_url": "https://example.test/Omi_DK2_OTA_v2.0.10.zip",
                }
            ],
        )
    ]
    calls = []

    class FakeResponse:
        status_code = 200
        text = ""

        def __init__(self, payload):
            self._payload = payload

        def json(self):
            return self._payload

    class FakeAsyncClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, url, headers):
            calls.append((url, headers))
            query = parse_qs(urlparse(url).query)
            page = int(query["page"][0])
            return FakeResponse({1: first_page, 2: second_page}[page])

    cached = {}

    monkeypatch.setattr(firmware, "get_generic_cache", lambda _key: None)
    monkeypatch.setattr(firmware, "set_generic_cache", lambda key, value, ttl: cached.update({key: (value, ttl)}))
    monkeypatch.setattr(firmware.httpx, "AsyncClient", FakeAsyncClient)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)

    releases = asyncio.run(firmware.get_omi_github_releases("github_releases_omi"))

    assert len(releases) == 101
    assert releases[-1]["tag_name"] == "Omi_DK2_v2.0.10"
    assert [parse_qs(urlparse(url).query)["page"][0] for url, _headers in calls] == ["1", "2"]
    assert "Authorization" not in calls[0][1]
    assert cached["github_releases_omi"][1] == 300


def test_latest_firmware_can_resolve_release_after_unrelated_release_page(monkeypatch):
    body = """<!-- KEY_VALUE_START
release_firmware_version:2.0.10
minimum_firmware_required:2.0.2
minimum_app_version:1.0.55
minimum_app_version_code:242
ota_update_steps:no_usb,battery,internet
changelog:Fixed battery drain while in sleep mode|Fixed button functionality
KEY_VALUE_END -->"""
    releases = [_release(f"v0.11.{idx}+macos") for idx in range(100)]
    releases.append(
        _release(
            "Omi_DK2_v2.0.10",
            published_at="2025-04-26T11:38:30Z",
            body=body,
            assets=[
                {
                    "name": "Omi_DK2_OTA_v2.0.10.zip",
                    "browser_download_url": "https://example.test/Omi_DK2_OTA_v2.0.10.zip",
                }
            ],
        )
    )

    async def fake_get_releases(_cache_key):
        return releases

    monkeypatch.setattr(firmware, "get_omi_github_releases", fake_get_releases)

    result = asyncio.run(
        firmware.get_latest_version(
            device_model="Omi DevKit 2",
            firmware_revision="2.0.8",
            hardware_revision="1.0",
            manufacturer_name="Based Hardware",
        )
    )

    assert result["version"] == "2.0.10"
    assert result["zip_url"] == "https://example.test/Omi_DK2_OTA_v2.0.10.zip"
    assert result["ota_update_steps"] == ["no_usb", "battery", "internet"]
