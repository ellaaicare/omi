import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import pytest

from utils.stt.session_delivery import (
    ProviderAudioSendRejected,
    SttSessionDeliveryReceipt,
    forward_async_provider_audio,
    forward_deepgram_audio,
)

BACKEND = Path(__file__).resolve().parents[2]


class _Socket:
    def __init__(self, accepted):
        self.accepted = accepted
        self.chunks = []

    def send(self, chunk):
        self.chunks.append(chunk)
        return self.accepted


def test_delivery_receipt_tracks_every_content_free_boundary():
    receipt = SttSessionDeliveryReceipt()
    receipt.record_ingress(72)
    receipt.record_decoded_pcm(b"\x00\x00\x00\x01")
    receipt.record_provider_send(4, accepted=True)
    receipt.record_provider_event("open")
    receipt.record_provider_event("empty")
    receipt.record_provider_event("interim")
    receipt.record_provider_event("final")
    receipt.record_transcript_callback(2)
    receipt.drain_requested = True
    receipt.drain_completed = True

    snapshot = receipt.snapshot(terminal_reason="capture_drained")

    assert snapshot == {
        "ingress_frames": 1,
        "ingress_encoded_bytes": 72,
        "decoded_frames": 1,
        "decoded_pcm_bytes": 4,
        "decode_errors": 0,
        "pcm_peak_abs": 256,
        "signal_frames_above_floor": 1,
        "provider_send_attempts": 1,
        "provider_send_accepted": 1,
        "provider_send_rejected": 0,
        "provider_pcm_bytes_accepted": 4,
        "provider_open_events": 1,
        "provider_empty_results": 1,
        "provider_interim_results": 1,
        "provider_final_results": 1,
        "provider_error_events": 0,
        "provider_close_events": 0,
        "transcript_callbacks": 1,
        "transcript_segments": 2,
        "drain_requested": True,
        "drain_completed": True,
        "terminal_reason": "capture_drained",
    }
    assert not any(key in snapshot for key in ("uid", "text", "transcript", "audio", "pcm"))


def test_deepgram_send_rejection_is_terminal_and_counted():
    receipt = SttSessionDeliveryReceipt()
    socket = _Socket(False)

    with pytest.raises(ProviderAudioSendRejected, match="stt_provider_send_rejected"):
        forward_deepgram_audio(socket, b"\x00\x00", receipt)

    assert socket.chunks == [b"\x00\x00"]
    assert receipt.provider_send_attempts == 1
    assert receipt.provider_send_accepted == 0
    assert receipt.provider_send_rejected == 1
    assert receipt.provider_pcm_bytes_accepted == 0


def test_deepgram_send_acceptance_is_counted():
    receipt = SttSessionDeliveryReceipt()

    forward_deepgram_audio(_Socket(True), b"\x00\x00", receipt)

    assert receipt.provider_send_attempts == 1
    assert receipt.provider_send_accepted == 1
    assert receipt.provider_send_rejected == 0
    assert receipt.provider_pcm_bytes_accepted == 2


def test_async_provider_send_acceptance_is_counted():
    receipt = SttSessionDeliveryReceipt()

    async def send(_chunk):
        return None

    asyncio.run(forward_async_provider_audio(send, b"\x00\x00", receipt))

    assert receipt.provider_send_attempts == 1
    assert receipt.provider_send_accepted == 1
    assert receipt.provider_send_rejected == 0
    assert receipt.provider_pcm_bytes_accepted == 2


def test_async_provider_send_failure_is_terminal_and_counted():
    receipt = SttSessionDeliveryReceipt()

    async def send(_chunk):
        raise ConnectionError("content-bearing-error")

    with pytest.raises(ConnectionError, match="content-bearing-error"):
        asyncio.run(forward_async_provider_audio(send, b"\x00\x00", receipt))

    assert receipt.provider_send_attempts == 1
    assert receipt.provider_send_accepted == 0
    assert receipt.provider_send_rejected == 1
    assert receipt.provider_pcm_bytes_accepted == 0


def test_progress_receipt_is_rate_limited():
    receipt = SttSessionDeliveryReceipt(_last_progress_at=10.0)

    assert receipt.progress_due(now=39.9) is False
    assert receipt.progress_due(now=40.0) is True
    assert receipt.progress_due(now=69.9) is False


def _load_streaming(monkeypatch):
    monkeypatch.setenv("DEEPGRAM_API_KEY", "test-only-key")
    monkeypatch.setitem(sys.modules, "utils.stt.soniox_util", types.ModuleType("utils.stt.soniox_util"))

    class _ClientOptions:
        def __init__(self, options=None):
            self.options = options or {}
            self.url = None

    class _LiveOptions:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class _DeepgramClient:
        def __init__(self, *_args, **_kwargs):
            self.listen = types.SimpleNamespace()

    events = types.SimpleNamespace(
        Transcript="transcript",
        Error="error",
        Open="open",
        Metadata="metadata",
        SpeechStarted="speech_started",
        UtteranceEnd="utterance_end",
        Close="close",
        Unhandled="unhandled",
    )
    deepgram_module = types.ModuleType("deepgram")
    deepgram_module.DeepgramClient = _DeepgramClient
    deepgram_module.DeepgramClientOptions = _ClientOptions
    deepgram_module.LiveTranscriptionEvents = events
    monkeypatch.setitem(sys.modules, "deepgram", deepgram_module)
    clients_module = types.ModuleType("deepgram.clients")
    live_module = types.ModuleType("deepgram.clients.live")
    v1_module = types.ModuleType("deepgram.clients.live.v1")
    v1_module.LiveOptions = _LiveOptions
    monkeypatch.setitem(sys.modules, "deepgram.clients", clients_module)
    monkeypatch.setitem(sys.modules, "deepgram.clients.live", live_module)
    monkeypatch.setitem(sys.modules, "deepgram.clients.live.v1", v1_module)

    spec = importlib.util.spec_from_file_location(
        "stt_streaming_delivery_test_module",
        BACKEND / "utils" / "stt" / "streaming.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_deepgram_callbacks_report_empty_error_and_close_without_error_content(monkeypatch, capsys):
    streaming = _load_streaming(monkeypatch)

    class _Connection:
        def __init__(self):
            self.handlers = {}
            self.started_options = None

        def on(self, event, handler):
            self.handlers[event] = handler

        def start(self, options):
            self.started_options = options
            return True

    connection = _Connection()
    websocket = types.SimpleNamespace(v=lambda version: connection)
    streaming.deepgram = types.SimpleNamespace(listen=types.SimpleNamespace(websocket=websocket))
    events = []
    segments = []
    asyncio.run(
        streaming.process_audio_dg(
            segments.extend,
            "en",
            16000,
            1,
            model="nova-3",
            keywords=[],
            delivery_event_callback=events.append,
        )
    )

    alternative = types.SimpleNamespace(transcript="", words=[])
    result = types.SimpleNamespace(channel=types.SimpleNamespace(alternatives=[alternative]))
    connection.handlers[streaming.LiveTranscriptionEvents.Transcript](None, result)
    connection.handlers[streaming.LiveTranscriptionEvents.Error](None, RuntimeError("secret-bearing-detail"))
    connection.handlers[streaming.LiveTranscriptionEvents.Close](None, object())

    assert [event["result_type"] for event in events] == ["empty", "error", "closed"]
    assert events[1]["error_class"] == "RuntimeError"
    assert connection.started_options.interim_results is False
    assert "secret-bearing-detail" not in capsys.readouterr().out


def test_production_route_wires_receipt_and_does_not_log_transcript_text():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    streaming_source = (BACKEND / "utils" / "stt" / "streaming.py").read_text()

    assert "delivery_receipt.record_ingress(len(data))" in source
    assert "delivery_receipt.record_decoded_pcm(bytes(data))" in source
    assert "forward_deepgram_audio(dg_socket, chunk, delivery_receipt)" in source
    assert "forward_deepgram_audio(deepgram_socket, data, delivery_receipt)" in source
    assert "forward_async_provider_audio(soniox_sock.send, chunk, delivery_receipt)" in source
    assert "forward_async_provider_audio(speechmatics_sock.send, chunk, delivery_receipt)" in source
    assert '"[STT-DELIVERY]' in source
    assert 'text=event.get("text")' not in source
    assert '"text": sentence[:120]' not in streaming_source
    assert "delivery_event_callback=_provider_delivery_event_callback," in source


def test_custom_stt_receipt_uses_client_provider_authority():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    assert 'delivery_provider_override = "client" if custom_stt_mode == CustomSttMode.enabled else None' in source
    assert '"provider": delivery_provider_override or _stt_service_value(selected_stt_service)' in source


def test_deepgram_fallbacks_report_the_effective_provider():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    soniox_fallback = source.index("Soniox unavailable")
    soniox_deepgram = source.index("deepgram_socket = await process_audio_dg", soniox_fallback)
    grok_fallback = source.index("Grok STT selected but disabled")
    grok_deepgram = source.index("deepgram_socket = await process_audio_dg", grok_fallback)

    assert "stt_service = STTService.deepgram" in source[soniox_fallback:soniox_deepgram]
    assert "selected_stt_service = STTService.deepgram" in source[soniox_fallback:soniox_deepgram]
    assert "selected_stt_model = 'nova-3'" in source[soniox_fallback:soniox_deepgram]
    assert "stt_service = STTService.deepgram" in source[grok_fallback:grok_deepgram]
    assert "selected_stt_service = STTService.deepgram" in source[grok_fallback:grok_deepgram]
    assert "selected_stt_model = 'nova-2-general'" in source[grok_fallback:grok_deepgram]
