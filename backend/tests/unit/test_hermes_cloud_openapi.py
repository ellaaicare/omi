from pathlib import Path

import yaml

from database.runtime_targets import CLOUD_RUNTIME_TARGET_MODES
from ella.services import ai_consent


def test_openapi_covers_every_reviewed_runtime_boundary_with_exact_targets():
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "ella" / "docs" / "hermes-cloud-runtime-targets.openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    paths = contract["paths"]
    exact_modes = {
        "/v1/ella/resolve": "hermes-cloud-chat",
        "/v1/ella/onboarding/ensure": "hermes-cloud-chat",
        "/v1/ella/onboarding/status": "hermes-cloud-chat",
        "/v1/ella/chat/stream": "hermes-cloud-chat",
        "/v1/ella/chat/history": "hermes-cloud-chat",
        "/v1/voice/session": "hermes-cloud-voice",
        "/v1/voice/context": "hermes-cloud-voice",
        "/v1/voice/tool": "hermes-cloud-voice",
        "/v1/voice/search-omi": "hermes-cloud-voice",
        "/v1/voice/search": "hermes-cloud-voice",
        "/v4/listen": "hermes-cloud-transcript",
        "/v4/web/listen": "hermes-cloud-transcript",
        "/v1/ella/conversation/{conversation_id}/summary": "hermes-cloud-transcript",
        "/v1/conversations/{conversation_id}/processing-retries": "hermes-cloud-transcript",
        "/v1/ella/observer/run": "hermes-cloud-guardian",
        "/v1/ella/guardian/next-audio": "hermes-cloud-guardian",
    }
    for path, mode in exact_modes.items():
        operation = next(iter(paths[path].values()))
        assert operation["x-ella-runtime-target"]["mode"] == mode
        assert operation["x-ella-runtime-target"]["fallback"] == "none"

    invitation = paths["/v1/invite/redeem"]["post"]["x-ella-runtime-target"]
    assert tuple(invitation["publishes-modes"]) == CLOUD_RUNTIME_TARGET_MODES
    assert invitation["fallback"] == "none"


def test_openapi_processor_disclosure_uses_only_exact_v8_processor_ids():
    contract = yaml.safe_load(
        (Path(__file__).resolve().parents[2] / "ella" / "docs" / "hermes-cloud-runtime-targets.openapi.yaml").read_text(
            encoding="utf-8"
        )
    )
    processors = {str(processor["id"]) for processor in ai_consent.PROCESSORS if isinstance(processor, dict)}
    paths = contract["paths"]
    voice = paths["/v1/voice/session"]["post"]["x-ella-ai-data-processors-v8"]
    listen = paths["/v4/listen"]["get"]["x-ella-ai-data-processors-v8"]
    web_listen = paths["/v4/web/listen"]["get"]["x-ella-ai-data-processors-v8"]

    assert set(voice["always"]) | set(voice["selected-live-voice-provider-one-of"]) <= processors
    assert set(listen["always"]) | set(listen["selected-stt-provider-one-of"]) <= processors
    assert web_listen == listen
    assert contract["components"]["schemas"]["AiConsentPolicy"]["properties"]["version"]["const"] == (
        ai_consent.CURRENT_POLICY_VERSION
    )
