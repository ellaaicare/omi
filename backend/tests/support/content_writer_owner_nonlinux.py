"""Non-production process adapter for running unit tests on Darwin."""

from __future__ import annotations

import hashlib
import socket
import subprocess

from database import content_writer_owner

_PS = "/bin/ps"
_SYSCTL = "/usr/sbin/sysctl"


def current_process_boundary() -> content_writer_owner.ProcessBoundary:
    boot = subprocess.run(
        [_SYSCTL, "-n", "kern.boottime"],
        check=True,
        capture_output=True,
        text=True,
        timeout=2,
    ).stdout.strip()
    host = socket.gethostname().strip()
    return content_writer_owner.ProcessBoundary(
        system="Linux",
        host_id=hashlib.sha256(f"darwin-test-only:{host}".encode("utf-8")).hexdigest(),
        boot_id=f"darwin-test-only:{boot}",
        pid_namespace="darwin-test-only-process-table",
    )


def _ps_value(pid: int, field: str) -> str | None:
    completed = subprocess.run(
        [_PS, "-o", f"{field}=", "-p", str(pid)],
        check=False,
        capture_output=True,
        text=True,
        timeout=2,
    )
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def process_snapshot(system: str, pid: int) -> content_writer_owner.ProcessSnapshot | None:
    assert system == "Linux"
    first_start = _ps_value(pid, "lstart")
    if first_start is None:
        return None
    state = _ps_value(pid, "state")
    second_start = _ps_value(pid, "lstart")
    if state is None or second_start is None or first_start != second_start:
        raise content_writer_owner.ProcessOwnerError("account_writer_process_state_unknown")
    return content_writer_owner.ProcessSnapshot(start_id=f"darwin-test-only:{first_start}", state=state[:1])
