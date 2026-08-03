import json
import os
import time
from typing import AsyncIterator

from fastapi import Depends, Header, HTTPException
from fastapi import Request
from firebase_admin import auth
from firebase_admin.auth import InvalidIdTokenError, UserNotFoundError

from database import content_write_fence

# Retained import surface used by focused Guardian collection and test overrides.
voice_canary = content_write_fence.voice_canary


def get_user(uid: str):
    user = auth.get_user(uid)
    return user


def verify_token(token: str) -> str:
    """
    Verify a Firebase token or ADMIN_KEY and return the uid.

    Args:
        token: The token to verify (Firebase ID token or ADMIN_KEY format)

    Returns:
        The user's uid

    Raises:
        InvalidIdTokenError: If the token is invalid
    """
    # Check for ADMIN_KEY format
    admin_key = os.getenv('ADMIN_KEY')
    if admin_key and admin_key in token:
        return token.split(admin_key)[1]

    # Verify Firebase token
    try:
        decoded_token = auth.verify_id_token(token)
        return decoded_token['uid']
    except InvalidIdTokenError:
        if os.getenv('LOCAL_DEVELOPMENT') == 'true':
            return '123'
        raise
    except Exception as e:
        print(f"Token verification error: {type(e).__name__}: {e}", flush=True)
        if os.getenv('LOCAL_DEVELOPMENT') == 'true':
            return '123'
        raise InvalidIdTokenError(str(e))


def get_authenticated_user_uid(authorization: str = Header(None)) -> str:
    """Verify the Firebase subject without consulting optional Ella storage."""
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header not found")
    elif len(str(authorization).split(' ')) != 2:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    try:
        token = authorization.split(' ')[1]
        if not token:
            raise HTTPException(status_code=401, detail="Empty authorization token")
        return verify_token(token)
    except InvalidIdTokenError as e:
        print(f"Error verifying Firebase ID token: {e}", flush=True)
        raise HTTPException(status_code=401, detail="Invalid authorization token")


async def assert_authenticated_user_writable(uid: str) -> str:
    """Perform a compatibility preflight; committers must hold the dependency."""
    try:
        async with content_write_fence.content_write_fence(uid):
            return uid
    except content_write_fence.ContentWriteFenceError as exc:
        if exc.code == "account_write_forbidden":
            raise HTTPException(
                status_code=403,
                detail={"code": "account_write_forbidden", "retryable": False},
            ) from exc
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "retryable": True},
        ) from exc


async def admit_authenticated_content_writer(uid: str) -> str:
    """Admit a manually authenticated writer for the full ASGI request."""
    try:
        return await content_write_fence.admit_request_content_writer(uid)
    except content_write_fence.ContentWriteFenceError as exc:
        if exc.code == "account_write_forbidden":
            raise HTTPException(
                status_code=403,
                detail={"code": "account_write_forbidden", "retryable": False},
            ) from exc
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "retryable": True},
        ) from exc


def get_current_user_uid(authorization: str = Header(None)) -> str:
    """Backward-compatible raw Firebase authentication dependency."""
    return get_authenticated_user_uid(authorization)


async def get_writable_user_uid(uid: str = Depends(get_current_user_uid)) -> AsyncIterator[str]:
    """Hold the distributed deletion fence through the complete ASGI request."""
    try:
        async with content_write_fence.request_content_write_fence(uid):
            yield uid
    except content_write_fence.ContentWriteFenceError as exc:
        if exc.code == "account_write_forbidden":
            raise HTTPException(
                status_code=403,
                detail={"code": "account_write_forbidden", "retryable": False},
            ) from exc
        raise HTTPException(
            status_code=503,
            detail={"code": exc.code, "retryable": True},
        ) from exc


def get_current_user_uid_from_ws_message(message: dict) -> str:
    """
    Get user uid from WebSocket first-message auth.

    Expected message format: {"type": "auth", "token": "<token>"}

    Returns:
        The user's uid

    Raises:
        ValueError: If message format is invalid
        InvalidIdTokenError: If token is invalid
    """
    if message.get("type") == "websocket.disconnect":
        raise ValueError("Client disconnected")

    text = message.get("text")
    if text is None:
        raise ValueError("Expected JSON auth message")

    try:
        auth_data = json.loads(text)
    except json.JSONDecodeError:
        raise ValueError("Invalid JSON")

    if auth_data.get("type") != "auth":
        raise ValueError("First message must be auth")

    token = auth_data.get("token")
    if not token:
        raise ValueError("Missing token")

    return verify_token(token)


cached = {}


def rate_limit_custom(endpoint: str, request: Request, requests_per_window: int, window_seconds: int):
    ip = request.client.host
    key = f"rate_limit:{endpoint}:{ip}"

    # Check if the IP is already rate-limited
    current = cached.get(key)
    if current:
        current = json.loads(current)
        remaining = current["remaining"]
        timestamp = current["timestamp"]
        current_time = int(time.time())

        # Check if the time window has expired
        if current_time - timestamp >= window_seconds:
            remaining = requests_per_window - 1  # Reset the counter for the new window
            timestamp = current_time
        elif remaining == 0:
            raise HTTPException(status_code=429, detail="Too Many Requests")

        remaining -= 1

    else:
        # If no previous data found, start a new time window
        remaining = requests_per_window - 1
        timestamp = int(time.time())

    # Update the rate limit info in Redis
    current = {"timestamp": timestamp, "remaining": remaining}
    cached[key] = json.dumps(current)

    return True


# Dependency to enforce custom rate limiting for specific endpoints
def rate_limit_dependency(endpoint: str = "", requests_per_window: int = 60, window_seconds: int = 60):
    def rate_limit(request: Request):
        return rate_limit_custom(endpoint, request, requests_per_window, window_seconds)

    return rate_limit


def timeit(func):
    """
    Decorator for measuring function's running time.
    """

    def measure_time(*args, **kw):
        start_time = time.time()
        result = func(*args, **kw)
        print("Processing time of %s(): %.2f seconds." % (func.__qualname__, time.time() - start_time))
        return result

    return measure_time


def delete_account(uid: str):
    try:
        auth.delete_user(uid)
        return {"status": "deleted"}
    except UserNotFoundError:
        return {"status": "already_deleted"}
    except Exception:
        # A lost delete acknowledgement is outcome-ambiguous. Confirm absence
        # before treating it as success; otherwise preserve the retryable error.
        try:
            auth.get_user(uid)
        except UserNotFoundError:
            return {"status": "already_deleted"}
        raise
