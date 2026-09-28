import ast
import asyncio
import types
from pathlib import Path
from typing import Awaitable, Callable

import pytest
from fastapi import HTTPException

BACKEND = Path(__file__).resolve().parents[2]


def _gated_route_paths(source_path: Path, dependency_name: str) -> set[str]:
    tree = ast.parse(source_path.read_text())
    gated_paths = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        signature_uses_dependency = dependency_name in ast.unparse(node.args)
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            path_arg = decorator.args[0]
            if not isinstance(path_arg, ast.Constant) or not isinstance(path_arg.value, str):
                continue
            dependencies = next(
                (keyword.value for keyword in decorator.keywords if keyword.arg == "dependencies"),
                None,
            )
            decorator_uses_dependency = dependencies is not None and dependency_name in ast.unparse(dependencies)
            if signature_uses_dependency or decorator_uses_dependency:
                gated_paths.add(path_arg.value)

    return gated_paths


def _function_source(source_path: Path, function_name: str) -> str:
    source = source_path.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return ast.get_source_segment(source, function)


def _function_code(source_path: Path, function_name: str):
    pending = [compile(source_path.read_text(), str(source_path), "exec")]
    while pending:
        code = pending.pop()
        for constant in code.co_consts:
            if not isinstance(constant, types.CodeType):
                continue
            if constant.co_name == function_name:
                return constant
            pending.append(constant)
    raise AssertionError(f"function code not found: {function_name}")


def _class_from_source(source_path: Path, class_name: str, globals_: dict):
    tree = ast.parse(source_path.read_text())
    class_node = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == class_name)
    module = ast.Module(body=[class_node], type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(source_path), "exec"), globals_)
    return globals_[class_name]


def test_authenticated_ai_egress_routes_share_the_consent_gate():
    assert "/chat/stream" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "chat.py",
        "require_current_ai_consent",
    )
    assert "/ensure" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "onboarding.py",
        "require_current_ai_consent",
    )
    assert "/session" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "voice.py",
        "require_current_ai_consent",
    )
    assert "/v4/listen" in _gated_route_paths(
        BACKEND / "routers" / "transcribe.py",
        "get_exact_firebase_uid",
    )


def test_first_message_websocket_auth_checks_consent_before_streaming():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    auth_position = source.index("uid = auth.get_current_user_uid_from_ws_message(first_message)")
    consent_position = source.index("_require_current_ai_consent_for_websocket(", auth_position)
    stream_position = source.index("await _stream_handler(", consent_position)

    assert auth_position < consent_position < stream_position
    assert "AI_CONSENT_WEBSOCKET_CLOSE_CODE" in source
    assert "AI_CONSENT_WEBSOCKET_RETRY_CLOSE_CODE" in source


def test_native_websocket_consent_check_precedes_runtime_and_stt_provider_work():
    source = _function_source(BACKEND / "routers" / "transcribe.py", "listen_handler")

    consent_position = source.index("_require_current_ai_consent_for_websocket(")
    runtime_position = source.index("listen_runtime_gate(")
    stream_position = source.index("await _listen(")

    assert consent_position < runtime_position < stream_position
    helper_source = _function_source(
        BACKEND / "routers" / "transcribe.py",
        "_require_current_ai_consent_for_websocket",
    )
    assert "_ai_consent_websocket_contract" in helper_source


def test_websocket_consent_rejection_and_authority_outage_have_distinct_close_contracts():
    threadpool_calls = []

    async def run_in_threadpool(function, *args):
        threadpool_calls.append((function, args))
        return function(*args)

    globals_ = {
        "__builtins__": __builtins__,
        "HTTPException": HTTPException,
        "AI_CONSENT_AUTHORITY_UNAVAILABLE_CODE": "ai_consent_authority_unavailable",
        "AI_CONSENT_REQUIRED_CODE": "ai_consent_required",
        "AI_CONSENT_WEBSOCKET_CLOSE_CODE": 4403,
        "AI_CONSENT_WEBSOCKET_RETRY_CLOSE_CODE": 1013,
        "run_in_threadpool": run_in_threadpool,
        "_ai_consent_websocket_contract": lambda exc: (
            (1013, "ai_consent_authority_unavailable", True)
            if exc.status_code >= 500
            else (4403, "ai_consent_required", False)
        ),
    }
    helper = types.FunctionType(
        _function_code(
            BACKEND / "routers" / "transcribe.py",
            "_require_current_ai_consent_for_websocket",
        ),
        globals_,
    )

    class Socket:
        def __init__(self):
            self.accepted = False
            self.sent = []
            self.closed = None

        async def accept(self):
            self.accepted = True

        async def send_json(self, payload):
            self.sent.append(payload)

        async def close(self, *, code, reason):
            self.closed = (code, reason)

    terminal = Socket()

    def reject_terminal(_uid):
        raise HTTPException(status_code=403, detail={"code": "ai_consent_required"})

    globals_["assert_current_ai_consent"] = reject_terminal
    assert (
        asyncio.run(
            helper(
                terminal,
                "uid-a",
                accepted=False,
                send_auth_response=False,
            )
        )
        is False
    )
    assert terminal.accepted is True
    assert terminal.sent == []
    assert terminal.closed == (4403, "ai_consent_required")
    assert threadpool_calls == [(reject_terminal, ("uid-a",))]

    retryable = Socket()

    def reject_retryable(_uid):
        raise HTTPException(
            status_code=503,
            detail={"code": "ai_consent_authority_unavailable", "retryable": True},
        )

    globals_["assert_current_ai_consent"] = reject_retryable
    assert (
        asyncio.run(
            helper(
                retryable,
                "uid-a",
                accepted=True,
                send_auth_response=True,
            )
        )
        is False
    )
    assert retryable.accepted is False
    assert retryable.sent == [
        {
            "type": "auth_response",
            "success": False,
            "error": "ai_consent_authority_unavailable",
            "retryable": True,
        }
    ]
    assert retryable.closed == (1013, "ai_consent_authority_unavailable")
    assert threadpool_calls[-1] == (reject_retryable, ("uid-a",))


def test_active_stt_audio_stops_at_terminal_or_retryable_consent_boundary():
    provider_bytes = []

    def forward_deepgram_audio(_socket, data, _receipt):
        provider_bytes.append(data)

    async def forward_async_provider_audio(provider_send, data, _receipt):
        await provider_send(data)

    deepgram_forward = types.FunctionType(
        _function_code(
            BACKEND / "routers" / "transcribe.py",
            "_forward_deepgram_audio_with_current_consent",
        ),
        {"forward_deepgram_audio": forward_deepgram_audio},
    )
    async_forward = types.FunctionType(
        _function_code(
            BACKEND / "routers" / "transcribe.py",
            "_forward_async_provider_audio_with_current_consent",
        ),
        {"forward_async_provider_audio": forward_async_provider_audio},
    )
    pusher_forward = types.FunctionType(
        _function_code(
            BACKEND / "routers" / "transcribe.py",
            "_send_pusher_payload_with_current_consent",
        ),
        {},
    )
    scanner_calls = []

    class SyncCheckRejected(RuntimeError):
        def __init__(self, authority_error):
            self.authority_error = authority_error

    async def run_in_threadpool(function, *args, **kwargs):
        return function(*args, **kwargs)

    sync_provider_code = _function_code(
        BACKEND / "routers" / "transcribe.py",
        "_run_sync_provider_with_current_consent",
    )
    sync_provider_globals = {
        "AI_CONSENT_AUTHORITY_UNAVAILABLE_CODE": "ai_consent_authority_unavailable",
        "HTTPException": HTTPException,
        "_AiConsentSyncCheckRejected": SyncCheckRejected,
        "run_in_threadpool": run_in_threadpool,
    }
    sync_provider_call = types.FunctionType(sync_provider_code, sync_provider_globals)

    state = {"failure": None}

    async def consent_guard():
        if state["failure"] is not None:
            raise state["failure"]

    async def provider_send(data):
        provider_bytes.append(data)

    def consent_checker(_uid):
        if state["failure"] is not None:
            raise state["failure"]

    async def reject_consent(exc):
        return exc

    def scanner_send(**kwargs):
        scanner_calls.append(kwargs)

    async def scenario():
        await deepgram_forward(consent_guard, object(), b"accepted-deepgram", object())
        await async_forward(consent_guard, provider_send, b"accepted-async", object())
        await pusher_forward(consent_guard, provider_send, b"accepted-pusher")
        await sync_provider_call(
            "uid-a",
            consent_checker,
            reject_consent,
            scanner_send,
            uid="uid-a",
            conversation_id="conversation-a",
            segments=[{"id": "segment-a"}],
            latency_metadata={"phase": "live"},
        )
        assert provider_bytes == [b"accepted-deepgram", b"accepted-async", b"accepted-pusher"]
        assert scanner_calls == [
            {
                "uid": "uid-a",
                "conversation_id": "conversation-a",
                "segments": [{"id": "segment-a"}],
                "latency_metadata": {"phase": "live"},
            }
        ]

        state["failure"] = HTTPException(
            status_code=403,
            detail={"code": "ai_consent_required"},
        )
        with pytest.raises(HTTPException) as terminal:
            await deepgram_forward(consent_guard, object(), b"revoked", object())
        assert terminal.value.status_code == 403
        with pytest.raises(HTTPException):
            await pusher_forward(consent_guard, provider_send, b"buffered-before-revoke")
        with pytest.raises(HTTPException):
            await sync_provider_call(
                "uid-a",
                consent_checker,
                reject_consent,
                scanner_send,
                uid="uid-a",
                conversation_id="conversation-a",
                segments=[{"id": "segment-revoked"}],
                latency_metadata={},
            )
        assert provider_bytes == [b"accepted-deepgram", b"accepted-async", b"accepted-pusher"]
        assert len(scanner_calls) == 1

        worker_queued = asyncio.Event()
        worker_release = asyncio.Event()
        delayed_provider_calls = []

        async def delayed_worker(function, *args, **kwargs):
            worker_queued.set()
            await worker_release.wait()
            return function(*args, **kwargs)

        delayed_sync_provider_call = types.FunctionType(
            sync_provider_code,
            {**sync_provider_globals, "run_in_threadpool": delayed_worker},
        )
        state["failure"] = None
        delayed_task = asyncio.create_task(
            delayed_sync_provider_call(
                "uid-a",
                consent_checker,
                reject_consent,
                lambda: delayed_provider_calls.append("sent"),
            )
        )
        await worker_queued.wait()
        state["failure"] = HTTPException(
            status_code=403,
            detail={"code": "ai_consent_required"},
        )
        worker_release.set()
        with pytest.raises(HTTPException) as saturated_worker_rejection:
            await delayed_task
        assert saturated_worker_rejection.value.status_code == 403
        assert delayed_provider_calls == []

        state["failure"] = HTTPException(
            status_code=503,
            detail={"code": "ai_consent_authority_unavailable", "retryable": True},
        )
        with pytest.raises(HTTPException) as retryable:
            await async_forward(consent_guard, provider_send, b"uncertain", object())
        assert retryable.value.status_code == 503
        with pytest.raises(HTTPException):
            await pusher_forward(consent_guard, provider_send, b"buffered-before-outage")
        with pytest.raises(HTTPException):
            await sync_provider_call(
                "uid-a",
                consent_checker,
                reject_consent,
                scanner_send,
                uid="uid-a",
                conversation_id="conversation-a",
                segments=[{"id": "segment-uncertain"}],
                latency_metadata={},
            )
        assert provider_bytes == [b"accepted-deepgram", b"accepted-async", b"accepted-pusher"]
        assert len(scanner_calls) == 1

    asyncio.run(scenario())

    stream_source = _function_source(BACKEND / "routers" / "transcribe.py", "_stream_handler")
    assert "_forward_deepgram_audio_with_current_consent(" in stream_source
    assert "_forward_async_provider_audio_with_current_consent(" in stream_source
    assert "_send_pusher_payload_with_current_consent(" in stream_source
    assert stream_source.count("await send_pusher_payload(data)") == 5
    assert "lambda: stt_egress_consent_guard(refresh=True)" in stream_source
    assert "segment_buffers.clear()" in stream_source
    assert stream_source.count("await _run_sync_provider_with_current_consent(") == 2
    assert "scanner_dispatch_queue.enqueue(" in stream_source
    assert "_dispatch_scanner_with_current_consent(" in stream_source
    assert stream_source.index("scanner_dispatch_queue.enqueue(") < stream_source.index(
        "await websocket.send_json([segment.dict() for segment in updated_segments])"
    )
    assert "await scanner_dispatch_queue.close()" in stream_source
    translate_source = _function_source(BACKEND / "routers" / "transcribe.py", "translate")
    speaker_source = _function_source(BACKEND / "routers" / "transcribe.py", "_match_speaker_embedding")
    for source, provider_name in (
        (translate_source, "translation_service.translate_text_by_sentence"),
        (speaker_source, "extract_embedding_from_bytes"),
    ):
        assert source.index("_run_sync_provider_with_current_consent(") < source.rindex(provider_name)
        assert source.index("assert_current_ai_consent") < source.rindex(provider_name)
    scanner_source = _function_source(
        BACKEND / "routers" / "transcribe.py",
        "_dispatch_scanner_with_current_consent",
    )
    assert "await _run_sync_provider_with_current_consent(" in scanner_source
    assert "await mode_loader(subject_uid)" in scanner_source
    assert "guardian_mode=guardian_mode" in scanner_source
    assert "except AiConsentWebSocketRejected" in scanner_source
    assert "except Exception" in scanner_source
    assert "audio_bytes_send(data, last_audio_received_time)" in stream_source
    assert "except AiConsentWebSocketRejected" in stream_source
    assert "not ai_consent_egress_rejected.is_set()" in stream_source


def test_scanner_dispatch_queue_is_bounded_ordered_and_drains_on_close():
    queue_class = _class_from_source(
        BACKEND / "routers" / "transcribe.py",
        "ScannerDispatchQueue",
        {
            "asyncio": asyncio,
            "Awaitable": Awaitable,
            "Callable": Callable,
            "SCANNER_DISPATCH_QUEUE_MAXSIZE": 32,
            "SCANNER_DISPATCH_DRAIN_TIMEOUT_SECONDS": 1.0,
            "SCANNER_EMERGENCY_CONTEXT_MAX_AGE_SECONDS": 10.0,
            "_SCANNER_DISPATCH_STOP": object(),
            "time": __import__("time"),
        },
    )

    async def scenario():
        dispatched = []
        dispatch_context = {}
        started = asyncio.Event()
        release = asyncio.Event()

        async def dispatch(item):
            dispatched.append((item["uid"], item["session"], item["sequence"]))
            dispatch_context[item["sequence"]] = item.get("recent_segments")
            if item["sequence"] == 1:
                started.set()
                await release.wait()

        queue = queue_class(
            dispatch,
            maxsize=3,
            drain_timeout_seconds=1.0,
            emergency_predicate=lambda item: item.get("kind") == "emergency",
        )
        queue.start()
        assert (
            queue.enqueue(
                {
                    "uid": "uid-1",
                    "conversation_id": "conversation-1",
                    "session": "session-1",
                    "sequence": 1,
                    "segments": [{"text": "I cannot", "speaker": "SPEAKER_1"}],
                }
            )
            is True
        )
        await started.wait()
        assert (
            queue.enqueue({"uid": "uid-1", "conversation_id": "conversation-1", "session": "session-1", "sequence": 2})
            is True
        )
        assert (
            queue.enqueue({"uid": "uid-1", "conversation_id": "conversation-1", "session": "session-1", "sequence": 3})
            is True
        )
        assert (
            queue.enqueue({"uid": "uid-1", "conversation_id": "conversation-1", "session": "session-1", "sequence": 4})
            is False
        )
        assert (
            queue.enqueue(
                {
                    "uid": "uid-1",
                    "conversation_id": "conversation-1",
                    "session": "session-1",
                    "sequence": 99,
                    "kind": "emergency",
                    "segments": [{"text": "breathe", "speaker": "SPEAKER_1"}],
                }
            )
            is True
        )
        assert (
            queue.enqueue(
                {
                    "uid": "uid-1",
                    "conversation_id": "conversation-1",
                    "session": "session-1",
                    "sequence": 100,
                    "kind": "emergency",
                    "segments": [{"text": "Please call 911", "speaker": "SPEAKER_1"}],
                }
            )
            is True
        )
        assert (
            queue.enqueue(
                {
                    "uid": "uid-1",
                    "conversation_id": "conversation-1",
                    "session": "session-1",
                    "sequence": 101,
                    "kind": "emergency",
                    "segments": [{"text": "I am choking", "speaker": "SPEAKER_1"}],
                }
            )
            is True
        )
        assert (
            queue.enqueue(
                {
                    "uid": "uid-1",
                    "conversation_id": "conversation-1",
                    "session": "session-1",
                    "sequence": 102,
                    "kind": "emergency",
                    "segments": [{"text": "I am having a stroke", "speaker": "SPEAKER_1"}],
                }
            )
            is False
        )
        release.set()
        await queue.close()
        assert dispatched == [
            ("uid-1", "session-1", 1),
            ("uid-1", "session-1", 99),
            ("uid-1", "session-1", 100),
            ("uid-1", "session-1", 101),
            ("uid-1", "session-1", 2),
            ("uid-1", "session-1", 3),
        ]
        assert dispatch_context[99] == [{"text": "I cannot", "speaker": "SPEAKER_1"}]
        assert queue.enqueue({"uid": "uid-2", "session": "session-2", "sequence": 5}) is False

    asyncio.run(scenario())


def test_scanner_dispatch_queue_expires_retained_emergency_context():
    from utils.ella.scanner import credible_emergency_reason_with_context

    queue_class = _class_from_source(
        BACKEND / "routers" / "transcribe.py",
        "ScannerDispatchQueue",
        {
            "asyncio": asyncio,
            "Awaitable": Awaitable,
            "Callable": Callable,
            "SCANNER_DISPATCH_QUEUE_MAXSIZE": 32,
            "SCANNER_DISPATCH_DRAIN_TIMEOUT_SECONDS": 1.0,
            "SCANNER_EMERGENCY_CONTEXT_MAX_AGE_SECONDS": 10.0,
            "_SCANNER_DISPATCH_STOP": object(),
            "time": __import__("time"),
        },
    )
    now = [100.0]

    async def dispatch(_item):
        return None

    def emergency_predicate(item):
        reason, _used_context = credible_emergency_reason_with_context(
            item.get("recent_segments") or [],
            item.get("segments") or [],
        )
        return reason is not None

    queue = queue_class(
        dispatch,
        maxsize=4,
        emergency_predicate=emergency_predicate,
        context_max_age_seconds=10.0,
        clock=lambda: now[0],
    )
    assert queue.enqueue(
        {
            "conversation_id": "conversation-1",
            "segments": [{"text": "She said", "speaker": "SPEAKER_1"}],
        }
    )
    now[0] += 10.1
    assert queue.enqueue(
        {
            "conversation_id": "conversation-1",
            "segments": [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
        }
    )

    queued = [queue._queue.get_nowait(), queue._queue.get_nowait()]
    by_text = {entry[2]["segments"][0]["text"]: entry for entry in queued}
    assert by_text["She said"][2]["recent_segments"] == []
    direct_emergency = by_text["I cannot breathe"]
    assert direct_emergency[2]["recent_segments"] == []
    assert direct_emergency[3] is True


def test_live_scanner_queue_uses_server_owned_emergency_predicate():
    stream_source = _function_source(BACKEND / "routers" / "transcribe.py", "_stream_handler")
    predicate_source = _function_source(
        BACKEND / "routers" / "transcribe.py",
        "_scanner_dispatch_item_is_credible_emergency",
    )

    assert "emergency_predicate=_scanner_dispatch_item_is_credible_emergency" in stream_source
    assert "import send_to_scanner" not in stream_source
    assert "credible_emergency_reason_with_context" in predicate_source
    assert 'item.get("segments")' in predicate_source

    from utils.ella.scanner import credible_emergency_reason_with_context

    namespace = {"credible_emergency_reason_with_context": credible_emergency_reason_with_context}
    exec(predicate_source, namespace)
    predicate = namespace["_scanner_dispatch_item_is_credible_emergency"]
    assert predicate(
        {
            "segments": [
                {"text": "I can't breathe, please call 911 now", "speaker": "SPEAKER_1"},
            ]
        }
    )
    assert predicate(
        {
            "segments": [
                {"text": "I am having a heart attack, call an ambulance now", "speaker": "SPEAKER_1"},
            ]
        }
    )
    # A disqualified same-speaker group (reported speech) must not shadow an
    # independent qualifying group later in the same batch.
    assert predicate(
        {
            "segments": [
                {"text": "I cannot breathe", "speaker": "SPEAKER_1"},
                {"text": "I cannot breathe", "speaker": "SPEAKER_2"},
            ],
            "recent_segments": [{"text": "She said.", "speaker": "SPEAKER_1"}],
        }
    )
    assert not predicate(
        {
            "segments": [{"text": "I cannot breathe", "speaker": "SPEAKER_1"}],
            "recent_segments": [{"text": "She said.", "speaker": "SPEAKER_1"}],
        }
    )


def test_scanner_mode_authority_uses_shared_async_pool_and_fails_closed():
    source = _function_source(
        BACKEND / "routers" / "transcribe.py",
        "_load_authoritative_guardian_mode",
    )

    assert "await get_ella_postgres_pool()" in source
    assert "await pool.fetchrow(" in source
    assert "psycopg2" not in source
    assert 'return None, "lookup_unavailable"' in source
    assert 'return None, "user_not_found"' in source

    class Pool:
        def __init__(self, row=None, failure=None):
            self.row = row
            self.failure = failure
            self.calls = []

        async def fetchrow(self, query, uid):
            self.calls.append((query, uid))
            if self.failure:
                raise self.failure
            return self.row

    async def scenario():
        code = _function_code(
            BACKEND / "routers" / "transcribe.py",
            "_load_authoritative_guardian_mode",
        )

        available_pool = Pool({"guardian_mode": "active_support"})

        async def available():
            return available_pool

        loader = types.FunctionType(code, {"get_ella_postgres_pool": available})
        assert await loader("uid-a") == ("active_support", None)
        assert available_pool.calls[0][1] == "uid-a"

        missing_pool = Pool()

        async def missing():
            return missing_pool

        loader = types.FunctionType(code, {"get_ella_postgres_pool": missing})
        assert await loader("uid-b") == (None, "user_not_found")

        unavailable_pool = Pool(failure=RuntimeError("database unavailable"))

        async def unavailable():
            return unavailable_pool

        loader = types.FunctionType(code, {"get_ella_postgres_pool": unavailable})
        assert await loader("uid-c") == (None, "lookup_unavailable")

    asyncio.run(scenario())


def test_stt_session_authority_avoids_transaction_per_audio_fragment_and_caches_fail_closed_state():
    calls = []
    state = {"failure": None}

    async def run_in_threadpool(function, *args):
        return function(*args)

    def checker(uid):
        calls.append(uid)
        if state["failure"] is not None:
            raise state["failure"]
        return uid

    authority_class = _class_from_source(
        BACKEND / "routers" / "transcribe.py",
        "AiConsentSessionAuthority",
        {
            "asyncio": asyncio,
            "Awaitable": Awaitable,
            "Callable": Callable,
            "HTTPException": HTTPException,
            "AI_CONSENT_AUTHORITY_UNAVAILABLE_CODE": "ai_consent_authority_unavailable",
            "AI_CONSENT_SESSION_REFRESH_SECONDS": 1.0,
            "assert_current_ai_consent": checker,
            "run_in_threadpool": run_in_threadpool,
        },
    )

    async def scenario():
        authority = authority_class("uid-a", checker=checker, refresh_interval_seconds=0.01)
        await authority.require_current()
        for _ in range(100):
            await authority.require_current()
        assert calls == ["uid-a"]

        state["failure"] = HTTPException(
            status_code=503,
            detail={"code": "ai_consent_authority_unavailable", "retryable": True},
        )
        with pytest.raises(HTTPException):
            await authority.refresh()
        assert calls == ["uid-a", "uid-a"]

        with pytest.raises(HTTPException):
            await authority.require_current()
        assert calls == ["uid-a", "uid-a"]

    asyncio.run(scenario())

    guard_source = _function_source(
        BACKEND / "routers" / "transcribe.py",
        "require_stt_egress_consent",
    )
    assert "run_in_threadpool" not in guard_source
    assert "ai_consent_session_authority.require_current" in guard_source


def test_memory_artwork_provider_routes_share_the_consent_gate():
    protected_paths = {
        "/memory-artwork/libraries",
        "/memories/{memory_id}/artwork",
        "/memory-artwork/day/{day}",
        "/memory-artwork/backfill",
        "/memory-artwork/recovery/recent",
        "/memory-artwork/recovery/permanent",
    }

    assert protected_paths <= _gated_route_paths(
        BACKEND / "ella" / "routers" / "memory_artwork.py",
        "require_current_ai_consent",
    )


def test_tts_route_uses_authenticated_or_internal_service_gate():
    voice_source_path = BACKEND / "ella" / "routers" / "voice.py"
    assert "/tts" in _gated_route_paths(
        voice_source_path,
        "require_current_ai_consent_or_internal_tts",
    )
    voice_source = voice_source_path.read_text()
    assert "if resolve_processor(provider) is None:" in voice_source
    guardian_source = (BACKEND / "ella" / "routers" / "guardian.py").read_text()
    assert 'headers["X-Ella-Subject-Uid"] = req.uid' in guardian_source


def test_selected_stt_provider_must_map_to_disclosed_recipient():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    selection_position = source.index("selected_stt_service = stt_service")
    disclosure_position = source.index(
        'if resolve_processor(_stt_service_value(stt_service) or "") is None:',
        selection_position,
    )
    provider_connect_position = source.index("# DEEPGRAM", disclosure_position)

    assert selection_position < disclosure_position < provider_connect_position


def test_legacy_payload_routes_cannot_bypass_consent_gate():
    protected_paths = {
        "/v2/messages",
        "/v2/initial-message",
        "/v2/voice-messages",
        "/v2/voice-message/transcribe",
        "/v2/files",
        "/v1/files",
        "/v1/initial-message",
    }
    gated_paths = _gated_route_paths(
        BACKEND / "routers" / "chat.py",
        "require_current_ai_consent",
    )

    assert protected_paths <= gated_paths


def test_stored_transcript_processing_routes_require_current_consent():
    protected_paths = {
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/reprocess",
        "/v1/conversations/{conversation_id}/test-prompt",
        "/v1/conversations/merge",
    }
    gated_paths = _gated_route_paths(
        BACKEND / "routers" / "conversations.py",
        "require_current_ai_consent",
    )

    assert protected_paths <= gated_paths


def test_shared_conversation_processor_gates_target_uid_before_model_work():
    source_path = BACKEND / "utils" / "conversations" / "process_conversation.py"
    source = _function_source(source_path, "process_conversation_with_outcome")

    assert source.index("assert_current_ai_consent(uid)") < source.index("_get_structured(")
    for caller in ("integration.py", "workflow.py", "developer.py"):
        caller_source = (BACKEND / "routers" / caller).read_text()
        assert "process_conversation(" in caller_source or "process_conversation_with_outcome(" in caller_source


def test_background_ai_and_honcho_boundaries_recheck_consent_before_network_egress():
    chat_source = _function_source(BACKEND / "ella" / "routers" / "chat.py", "_produce_hermes_chat_events")
    cloud_chat_source = _function_source(
        BACKEND / "ella" / "routers" / "chat.py",
        "_stream_hermes_cloud_chat",
    )
    voice_source = _function_source(
        BACKEND / "ella" / "services" / "voice_honcho.py",
        "fetch_voice_honcho_context",
    )
    recovery_source = _function_source(
        BACKEND / "ella" / "services" / "summary_recovery.py",
        "invoke_hermes_recovery",
    )
    observer_source = _function_source(
        BACKEND / "ella" / "services" / "observer_extractor.py",
        "build_extraction_result",
    )

    assert chat_source.index("_assert_current_ai_consent_async(uid)") < chat_source.index("httpx.AsyncClient(")
    assert "before_provider_call=lambda: _assert_current_ai_consent_async(uid)" in cloud_chat_source
    assert voice_source.index("await run_in_threadpool(") < voice_source.index("httpx.AsyncClient(")
    assert recovery_source.index("assert_current_ai_consent(uid)") < recovery_source.index(
        "generate_summary_from_prompt("
    )
    assert observer_source.index("await run_in_threadpool(assert_current_ai_consent, uid)") < observer_source.index(
        "hermes_candidate_extraction("
    )


def test_stored_sync_gates_target_uid_before_deepgram_and_processing():
    source_path = BACKEND / "routers" / "sync.py"
    process_segment_source = _function_source(source_path, "process_segment")

    assert process_segment_source.index("assert_current_ai_consent(uid)") < process_segment_source.index(
        "deepgram_prerecorded("
    )
    gated_sync_paths = _gated_route_paths(source_path, "require_current_ai_consent")
    assert "/v1/sync-local-files" in gated_sync_paths
    assert "/v2/sync-local-files" in gated_sync_paths
    assert "/v2/sync-capture-manifest" in gated_sync_paths


def test_legacy_initial_message_helper_gates_every_model_call_path():
    source = _function_source(BACKEND / "routers" / "chat.py", "initial_message_util")

    assert source.index("assert_current_ai_consent(uid)") < source.index("initial_chat_message(")


def test_guardian_model_and_legacy_callback_tts_gate_target_uid():
    consolidate_source = _function_source(BACKEND / "ella" / "routers" / "guardian.py", "_consolidate_queue")
    notification_source = _function_source(BACKEND / "ella" / "routers" / "callbacks.py", "ella_notification")

    assert consolidate_source.index("assert_current_ai_consent(uid)") < consolidate_source.index("httpx.AsyncClient(")
    assert notification_source.index("assert_current_ai_consent(request.uid)") < notification_source.index(
        "_generate_tts_audio("
    )


def test_consent_authority_router_is_registered_outside_ella_feature_switch():
    main_source = (BACKEND / "main.py").read_text()
    ella_init_source = (BACKEND / "ella" / "__init__.py").read_text()

    assert "app.include_router(ai_consent.router)" in main_source
    assert "ai_consent_router" not in ella_init_source


def test_correction_submit_is_gated_but_receipt_and_undo_remain_available():
    gated_paths = _gated_route_paths(
        BACKEND / "ella" / "routers" / "corrections.py",
        "require_current_ai_consent",
    )

    assert "/v1/ella/conversations/{conversation_id}/corrections" in gated_paths
    assert "/v1/conversations/{conversation_id}/corrections" in gated_paths
    assert "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}" not in gated_paths
    assert "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo" not in gated_paths
