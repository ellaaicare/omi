"""Repository-wide unit-test adapters that cannot be selected by production."""

from __future__ import annotations

import platform

import pytest


@pytest.fixture(autouse=True)
def _nonlinux_content_writer_adapter(monkeypatch):
    if platform.system() == "Linux":
        yield
        return

    from database import content_write_recovery, content_writer_owner
    from tests.support import content_writer_owner_nonlinux

    previous_owner = content_writer_owner._process_owner
    monkeypatch.setattr(
        content_writer_owner,
        "current_process_boundary",
        content_writer_owner_nonlinux.current_process_boundary,
    )
    monkeypatch.setattr(
        content_writer_owner,
        "process_snapshot",
        content_writer_owner_nonlinux.process_snapshot,
    )

    def no_production_supervisor_adapter(_pid_namespace, _namespace_pid):
        raise content_writer_owner.ProcessOwnerError("account_writer_supervisor_authority_required")

    monkeypatch.setattr(
        content_writer_owner,
        "linux_supervisor_process_snapshot",
        no_production_supervisor_adapter,
    )
    monkeypatch.setattr(content_write_recovery.platform, "system", lambda: "Linux")
    content_writer_owner._process_owner = None
    try:
        yield
    finally:
        content_writer_owner._process_owner = previous_owner
