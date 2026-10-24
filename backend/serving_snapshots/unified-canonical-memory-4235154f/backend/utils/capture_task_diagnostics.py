"""Payload-free task completion observers; never wrap or replace task coroutines."""
import asyncio
import builtins
import hashlib
import json
import time
from datetime import datetime, timezone

TASK_LABELS = frozenset({
    "receive_data", "stream_transcript", "heartbeat", "record_usage",
    "conversation_lifecycle", "pending_conversations", "speaker_identification",
    "consent_monitor", "pusher", "speech_profile", "outer_operation",
})
_CLASSES = {
    ValueError: "ValueError", TypeError: "TypeError", KeyError: "KeyError",
    RuntimeError: "RuntimeError", AttributeError: "AttributeError",
    AssertionError: "AssertionError", TimeoutError: "TimeoutError",
    OSError: "OSError", ConnectionError: "ConnectionError",
    asyncio.CancelledError: "CancelledError",
}
for _name in ("ExceptionGroup", "BaseExceptionGroup"):
    _class = getattr(builtins, _name, None)
    if _class is not None:
        _CLASSES[_class] = _name


def closed_exception_class(error):
    return _CLASSES.get(type(error), "unknown_exception")


def _log(payload):
    print("[CAPTURE-TASK] " + json.dumps(payload, sort_keys=True), flush=True)


def _emit(label, error, correlation, emit):
    try:
        if label not in TASK_LABELS:
            return
        payload = {
            "event": "task_failed", "task": label,
            "exception_class": closed_exception_class(error),
            "correlation_sha256": correlation,
            "utc": datetime.now(timezone.utc).isoformat(),
            "monotonic_ns": time.monotonic_ns(),
        }
        emit(payload)
    except BaseException:
        # Observability cannot change the task result or original failure.
        pass


def observe_task_failures(tasks, session_id, *, emit=_log):
    try:
        correlation = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        for task, label in tasks:
            if label not in TASK_LABELS or label == "outer_operation":
                continue

            def completed(done, label=label):
                try:
                    if done.cancelled():
                        return
                    error = done.exception()
                    if error is not None:
                        _emit(label, error, correlation, emit)
                except BaseException:
                    pass

            task.add_done_callback(completed)
    except BaseException:
        pass


def record_operation_failure(error, session_id, *, emit=_log):
    try:
        correlation = hashlib.sha256(session_id.encode("utf-8")).hexdigest()
        _emit("outer_operation", error, correlation, emit)
    except BaseException:
        pass
