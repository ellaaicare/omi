"""Explicit canonical summary-handler registration without importing routers."""

from typing import Any, Awaitable, Callable, Optional

_handler: Optional[Callable[..., Awaitable[dict[str, Any]]]] = None


def register_summary_operation_handler(handler: Callable[..., Awaitable[dict[str, Any]]]) -> None:
    global _handler
    _handler = handler


def summary_operation_handler() -> Optional[Callable[..., Awaitable[dict[str, Any]]]]:
    return _handler
