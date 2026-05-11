from typing import Optional


def service_value(service) -> Optional[str]:
    return service.value if hasattr(service, "value") else service


def models_for_preferred_stt_service(preferred_service, configured_models: list[str]) -> list[str]:
    preferred = service_value(preferred_service)
    if preferred is None:
        return configured_models
    if preferred == "grok":
        return ["grok-stt"]
    if preferred == "soniox":
        return ["soniox-stt-rt"]
    if preferred == "deepgram":
        configured = [m for m in configured_models if m.startswith("dg-")]
        return configured or ["dg-nova-3", "dg-nova-2"]
    if preferred == "speechmatics":
        return ["speechmatics"]
    return configured_models


def select_stt_service_for_language(
    language: str,
    *,
    multi_lang_enabled: bool,
    preferred_service,
    configured_models: list[str],
    service_enum,
    soniox_languages: set[str],
    soniox_multi_languages: set[str],
    deepgram_nova3_languages: set[str],
    deepgram_nova3_multi_languages: set[str],
    deepgram_nova2_languages: set[str],
    deepgram_nova2_multi_languages: set[str],
    grok_languages: set[str] = None,
    grok_multi_languages: set[str] = None,
):
    _grok_languages = grok_languages or set()
    _grok_multi_languages = grok_multi_languages or set()
    for model in models_for_preferred_stt_service(preferred_service, configured_models):
        if model == "grok-stt":
            if multi_lang_enabled and language in _grok_multi_languages:
                return service_enum.grok, "multi", "grok-stt"
            if language in _grok_languages:
                return service_enum.grok, language, "grok-stt"
        elif model == "soniox-stt-rt":
            if multi_lang_enabled and language in soniox_multi_languages:
                return service_enum.soniox, "multi", "stt-rt-preview"
            if language in soniox_languages:
                return service_enum.soniox, language, "stt-rt-preview"
        elif model == "speechmatics":
            return service_enum.speechmatics, language, "speechmatics"
        elif model == "dg-nova-3":
            if multi_lang_enabled and language in deepgram_nova3_multi_languages:
                return service_enum.deepgram, "multi", "nova-3"
            if language in deepgram_nova3_languages:
                return service_enum.deepgram, language, "nova-3"
        elif model == "dg-nova-2":
            if multi_lang_enabled and language in deepgram_nova2_multi_languages:
                return service_enum.deepgram, "multi", "nova-2-general"
            if language in deepgram_nova2_languages:
                return service_enum.deepgram, language, "nova-2-general"

    if preferred_service is not None:
        return None, None, None
    return service_enum.grok, "en", "grok-stt"
