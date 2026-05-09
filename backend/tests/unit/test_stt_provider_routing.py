from enum import Enum

from utils.stt.provider_selection import select_stt_service_for_language


class _Service(str, Enum):
    deepgram = "deepgram"
    soniox = "soniox"
    speechmatics = "speechmatics"


_SONIOX_LANGUAGES = {"multi", "en"}
_DG_NOVA3_LANGUAGES = {"en"}
_DG_NOVA3_MULTI_LANGUAGES = {"multi", "en"}
_DG_NOVA2_LANGUAGES = {"th"}
_DG_NOVA2_MULTI_LANGUAGES = {"multi", "en"}


def _select(language, configured_models, preferred_service=None, multi_lang_enabled=True):
    return select_stt_service_for_language(
        language,
        multi_lang_enabled=multi_lang_enabled,
        preferred_service=preferred_service,
        configured_models=configured_models,
        service_enum=_Service,
        soniox_languages=_SONIOX_LANGUAGES,
        soniox_multi_languages=_SONIOX_LANGUAGES,
        deepgram_nova3_languages=_DG_NOVA3_LANGUAGES,
        deepgram_nova3_multi_languages=_DG_NOVA3_MULTI_LANGUAGES,
        deepgram_nova2_languages=_DG_NOVA2_LANGUAGES,
        deepgram_nova2_multi_languages=_DG_NOVA2_MULTI_LANGUAGES,
    )


def test_explicit_soniox_provider_overrides_deepgram_default():
    provider, language, model = _select("en", ["dg-nova-3"], preferred_service=_Service.soniox)

    assert provider == _Service.soniox
    assert language == "multi"
    assert model == "stt-rt-preview"


def test_explicit_deepgram_provider_uses_configured_deepgram_model():
    provider, language, model = _select("en", ["soniox-stt-rt", "dg-nova-2"], preferred_service=_Service.deepgram)

    assert provider == _Service.deepgram
    assert language == "multi"
    assert model == "nova-2-general"


def test_unsupported_explicit_provider_does_not_silently_fallback():
    provider, language, model = _select(
        "zz-unsupported",
        ["dg-nova-3"],
        preferred_service=_Service.soniox,
        multi_lang_enabled=False,
    )

    assert provider is None
    assert language is None
    assert model is None


def test_default_routing_still_uses_server_order():
    provider, language, model = _select("en", ["dg-nova-3", "soniox-stt-rt"])

    assert provider == _Service.deepgram
    assert language == "multi"
    assert model == "nova-3"
