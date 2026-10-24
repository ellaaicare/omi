"""Bounded metadata observations, with no caller-controlled log strings or IDs.

Response-ready is not delivery, cancellation, or a business-effect receipt.
"""

import contextvars
import functools
import json
import logging
import time
import uuid

HEADER = "X-Ella-Mcp-Observation-Id"
_request = contextvars.ContextVar("ella_mcp_observation", default=None)
_log = logging.getLogger("uvicorn.error.ella_mcp_observation")
_METHODS = {"initialize", "notifications/initialized", "tools/list", "tools/call", "ping"}
_TOOLS = {"get_current_conversation_by_id", "get_conversation_by_id", "get_conversations", "get_memories"}
_CODES = {-32600, -32601, -32602, -32603, -32700, -32000, -32001, -32002, -32010, -32011, -32012}


def _emit(event, observation_id, **metadata):
    # Only internal callers below construct these fixed-key records. Deliberately
    # omit request arguments, JSON-RPC IDs, principal/session values and messages.
    try:
        # Standard text journal handlers do not retain arbitrary LogRecord extras.
        # Put this bounded, whitelisted record in the message itself.
        _log.info("ella_mcp_observation %s", json.dumps({
            "event": event, "observation_id": observation_id, **metadata,
        }, sort_keys=True, separators=(",", ":"), allow_nan=False))
    except Exception:
        # A telemetry handler failure must not alter existing MCP behavior.
        pass


def observe_request(function):
    @functools.wraps(function)
    async def wrapped(*args, **kwargs):
        observation_id = str(uuid.uuid4())
        token = _request.set(observation_id)
        start = time.monotonic()
        _emit("request_entered", observation_id)
        try:
            response = await function(*args, **kwargs)
            # Never echo a supplied correlation header; this ID is server-owned.
            response.headers[HEADER] = observation_id
            _emit("response_ready", observation_id,
                  http_status=response.status_code, elapsed_ms=round((time.monotonic() - start) * 1000, 3))
            return response
        except BaseException as error:
            status = getattr(error, "status_code", None)
            _emit("request_raised", observation_id,
                  http_status=status if type(status) is int and 100 <= status <= 599 else None,
                  error_kind="http_exception" if type(status) is int else "unexpected_or_cancelled",
                  elapsed_ms=round((time.monotonic() - start) * 1000, 3))
            raise
        finally:
            _request.reset(token)
    return wrapped


def observe_rpc(function):
    @functools.wraps(function)
    def wrapped(user_id, message, session=None, **kwargs):
        observation_id = _request.get()
        if observation_id is None:
            return function(user_id, message, session, **kwargs)
        method = message.get("method") if isinstance(message, dict) else None
        label = method if isinstance(method, str) and method in _METHODS else "other"
        tool = None
        if label == "tools/call":
            params = message.get("params")
            name = params.get("name") if isinstance(params, dict) else None
            tool = name if isinstance(name, str) and name in _TOOLS else "other"
        rpc_observation_id = str(uuid.uuid4())
        start = time.monotonic()
        _emit("rpc_entered", observation_id, rpc_observation_id=rpc_observation_id,
              method=label, tool=tool)
        try:
            result = function(user_id, message, session, **kwargs)
            response = result[0]
            error = response.get("error") if isinstance(response, dict) else None
            code = error.get("code") if isinstance(error, dict) else None
            _emit("rpc_returned", observation_id, rpc_observation_id=rpc_observation_id,
                  method=label, tool=tool, result_kind="error" if error is not None else "success_or_notification",
                  error_code=code if type(code) is int and code in _CODES else None,
                  elapsed_ms=round((time.monotonic() - start) * 1000, 3))
            return result
        except BaseException:
            _emit("rpc_raised", observation_id, rpc_observation_id=rpc_observation_id,
                  method=label, tool=tool, error_kind="unexpected_or_cancelled",
                  elapsed_ms=round((time.monotonic() - start) * 1000, 3))
            raise
    return wrapped
