"""Shared fail-closed runtime errors without provider import cycles."""

from __future__ import annotations

from typing import Any, Optional


class ProvisioningError(RuntimeError):
    def __init__(self, code: str, *, retryable: bool, detail: Optional[dict[str, Any]] = None):
        super().__init__(code)
        self.code = code
        self.retryable = retryable
        self.detail = detail or {}
