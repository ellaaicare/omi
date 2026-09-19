import asyncio
import importlib.util
import sys
import time
import types
import uuid
from enum import Enum
from pathlib import Path

import pytest

from utils.stt.session_delivery import (
    ProviderAudioSendRejected,
    SttSessionDeliveryReceipt,
    forward_async_provider_audio,
    forward_deepgram_audio,
)

BACKEND = Path(__file__).resolve().parents[2]


def _nested_code(name):
    root = compile((BACKEND / "routers" / "transcribe.py").read_text(), "routers/transcribe.py", "exec")
    pending = [root]
    while pending:
        code = pending.pop()
        for constant in code.co_consts:
            if not isinstance(constant, types.CodeType):
                continue
            if constant.co_name == name:
                return constant
            pending.append(constant)
    raise AssertionError(f"nested production function not found: {name}")


def _cell(value):
    return (lambda: value).__closure__[0]


def _nested_function(name, globals_, cells):
    code = _nested_code(name)
    missing = set(code.co_freevars) - set(cells)
    assert not missing, f"missing closure values for {name}: {sorted(missing)}"
    closure = tuple(cells[freevar] for freevar in code.co_freevars)
    return types.FunctionType(code, {"__builtins__": __builtins__, **globals_}, name, closure=closure)


class _STTService(str, Enum):
    deepgram = "deepgram"
    soniox = "soniox"
    grok = "grok"
    speechmatics = "speechmatics"


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

    with pytest.raises(ProviderAudioSendRejected, match="stt_provider_send_failed"):
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


def test_soniox_setup_rejection_logs_and_raises_without_provider_detail(monkeypatch, capsys):
    streaming = _load_streaming(monkeypatch)
    monkeypatch.setenv("SONIOX_API_KEY", "test-only-soniox-key")
    provider_detail = "synthetic-content-bearing-provider-detail"

    class _SonioxSocket:
        def __init__(self):
            self.closed = False
            self.sent = []

        async def send(self, payload):
            self.sent.append(payload)

        async def recv(self):
            return streaming.json.dumps(
                {
                    "error_code": 429,
                    "error_message": provider_detail,
                }
            )

        async def close(self):
            self.closed = True

    socket = _SonioxSocket()

    async def connect(*_args, **_kwargs):
        return socket

    monkeypatch.setattr(streaming.websockets, "connect", connect)

    with pytest.raises(ValueError) as exc_info:
        asyncio.run(
            streaming.process_audio_soniox(
                lambda _segments: None,
                16000,
                "en",
                "fixture-uid",
            )
        )

    output = capsys.readouterr().out
    assert socket.closed is True
    assert len(socket.sent) == 1
    assert "code=429" in str(exc_info.value)
    assert "class=ValueError" in output
    assert provider_detail not in str(exc_info.value)
    assert provider_detail not in output
    assert "test-only-soniox-key" not in output


def test_production_route_wires_receipt_and_does_not_log_transcript_text():
    source = (BACKEND / "routers" / "transcribe.py").read_text()
    streaming_source = (BACKEND / "utils" / "stt" / "streaming.py").read_text()

    assert "delivery_receipt.record_ingress(len(data))" in source
    assert "delivery_receipt.record_decoded_pcm(bytes(data))" in source
    assert "forward_deepgram_audio(dg_socket, chunk, delivery_receipt)" in source
    assert "forward_deepgram_audio(deepgram_socket, data, delivery_receipt)" in source
    assert "forward_async_provider_audio(soniox_sock.send, chunk, delivery_receipt)" in source
    assert "forward_async_provider_audio(speechmatics_sock.send, chunk, delivery_receipt)" in source
    assert 'terminal_reason="profile_provider_send_rejected"' in source
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

    assert "selected_stt_service = STTService.deepgram" in source[soniox_fallback:soniox_deepgram]
    assert "selected_stt_model = 'nova-3'" in source[soniox_fallback:soniox_deepgram]
    assert "preseconds=0" in source[soniox_deepgram : source.index("# GROK", soniox_deepgram)]
    assert "selected_stt_service = STTService.deepgram" in source[grok_fallback:grok_deepgram]
    assert "selected_stt_model = 'nova-2-general'" in source[grok_fallback:grok_deepgram]
    assert "preseconds=0" in source[grok_deepgram : source.index("# SPEECHMATICS", grok_deepgram)]
    assert 'segment.setdefault("stt_provider", _stt_service_value(selected_stt_service))' in source


def test_soniox_partial_profile_start_closes_before_single_deepgram_fallback(capsys):
    events = []
    deepgram_kwargs = []
    deepgram_callbacks = []
    transcript_segments = []
    receipt = SttSessionDeliveryReceipt()

    class _SonioxSocket:
        def __init__(self):
            self.chunks = []
            self.closed = False

        async def send(self, chunk):
            events.append("soniox_send")
            self.chunks.append(chunk)

        async def close(self):
            events.append("soniox_primary_close")
            self.closed = True

    class _DeepgramSocket:
        def __init__(self):
            self.chunks = []

        def send(self, chunk):
            events.append("deepgram_send")
            self.chunks.append(chunk)
            return True

    soniox_socket = _SonioxSocket()
    deepgram_socket = _DeepgramSocket()
    soniox_calls = 0

    async def process_soniox(callback, *_args, **_kwargs):
        nonlocal soniox_calls
        soniox_calls += 1
        if soniox_calls == 1:
            events.append("soniox_primary_open")
            return soniox_socket
        events.append("soniox_profile_rejected")
        raise ValueError("synthetic-content-bearing-provider-detail")

    async def process_deepgram(callback, *_args, **kwargs):
        events.append("deepgram_open")
        deepgram_callbacks.append(callback)
        deepgram_kwargs.append(kwargs)
        return deepgram_socket

    async def exercise():
        selected_service = _cell(_STTService.soniox)

        def bind_capture_conversation(segment):
            segment["_capture_conversation_id"] = "fixture-conversation"
            return segment

        stream_cells = {
            "_latency_log": _cell(lambda *_args, **_kwargs: None),
            "bind_capture_conversation": _cell(bind_capture_conversation),
            "delivery_receipt": _cell(receipt),
            "first_stt_result_at": _cell(None),
            "realtime_segment_buffers": _cell(transcript_segments),
            "selected_stt_service": selected_service,
            "stt_connect_ready_at": _cell(1.0),
            "stt_connect_started_at": _cell(1.0),
        }
        stream_transcript = _nested_function(
            "stream_transcript",
            {
                "time": time,
                "uuid": uuid,
                "_elapsed_ms": lambda *_args: 0,
                "_stt_service_value": lambda service: service.value,
            },
            stream_cells,
        )

        class _WebSocket:
            async def close(self, **_kwargs):
                raise AssertionError("the valid fallback must not close the client")

        cells = {
            "_create_speech_profile_loader_task": _cell(lambda *_args, **_kwargs: "profile-loader"),
            "_latency_log": _cell(lambda *_args, **_kwargs: None),
            "_provider_delivery_event_callback": _cell(lambda *_args, **_kwargs: None),
            "_stt_event_callback": _cell(lambda *_args, **_kwargs: None),
            "codec": _cell("pcm16"),
            "deepgram_profile_socket": _cell(None),
            "deepgram_socket": _cell(None),
            "grok_socket": _cell(None),
            "include_speech_profile": _cell(True),
            "language": _cell("en"),
            "sample_rate": _cell(16000),
            "selected_stt_model": _cell("test-soniox"),
            "selected_stt_service": selected_service,
            "session_id": _cell("fixture-session"),
            "soniox_profile_socket": _cell(None),
            "soniox_socket": _cell(None),
            "speech_profile_complete": _cell(asyncio.Event()),
            "speech_profile_preseconds": _cell(0),
            "speech_profile_state": _cell({}),
            "speechmatics_socket": _cell(None),
            "stream_transcript": _cell(stream_transcript),
            "stt_connect_ready_at": _cell(None),
            "stt_connect_started_at": _cell(None),
            "stt_language": _cell("en"),
            "stt_model": _cell("test-soniox"),
            "stt_service": _cell(_STTService.soniox),
            "uid": _cell("fixture-uid"),
            "use_custom_stt": _cell(False),
            "vocabulary": _cell([]),
            "websocket": _cell(_WebSocket()),
            "websocket_active": _cell(True),
            "websocket_close_code": _cell(1001),
        }
        process_stt = _nested_function(
            "_process_stt",
            {
                "Exception": Exception,
                "SPEECH_PROFILE_FIXED_DURATION": 12,
                "SPEECH_PROFILE_PADDING_DURATION": 3,
                "SPEECH_PROFILE_STABILIZE_DELAY": 0,
                "STTService": _STTService,
                "STT_LATENCY_LOGS_ENABLED": False,
                "ValueError": ValueError,
                "_elapsed_ms": lambda *_args: 0,
                "_stt_service_value": lambda service: service.value,
                "get_user_has_speech_profile": lambda *_args: True,
                "process_audio_dg": process_deepgram,
                "process_audio_soniox": process_soniox,
                "process_audio_speechmatics": lambda *_args, **_kwargs: None,
                "time": time,
            },
            cells,
        )

        assert await process_stt() == "profile-loader"
        assert cells["soniox_socket"].cell_contents is None
        assert cells["soniox_profile_socket"].cell_contents is None
        assert cells["deepgram_socket"].cell_contents is deepgram_socket
        assert selected_service.cell_contents is _STTService.deepgram
        assert cells["selected_stt_model"].cell_contents == "nova-3"

        chunk = b"\x01\x00" * 480
        if cells["soniox_socket"].cell_contents is not None:
            await forward_async_provider_audio(cells["soniox_socket"].cell_contents.send, chunk, receipt)
        if cells["deepgram_socket"].cell_contents is not None:
            forward_deepgram_audio(cells["deepgram_socket"].cell_contents, chunk, receipt)
        deepgram_callbacks[0]([{"text": "fixture"}])

    asyncio.run(exercise())

    assert soniox_calls == 2
    assert soniox_socket.closed is True
    assert soniox_socket.chunks == []
    assert deepgram_socket.chunks == [b"\x01\x00" * 480]
    assert deepgram_kwargs[0]["preseconds"] == 0
    assert events.index("soniox_primary_close") < events.index("deepgram_open") < events.index("deepgram_send")
    assert transcript_segments[0]["stt_provider"] == "deepgram"
    assert "synthetic-content-bearing-provider-detail" not in capsys.readouterr().out
