"""Immutable process ownership and kernel-backed terminal proof for writers."""

from __future__ import annotations

from dataclasses import dataclass
import errno
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import threading
from typing import Any
import uuid

OWNER_SCHEMA_VERSION = 1
SUPPORTED_RECOVERY_SYSTEMS = {"Linux", "Darwin"}
LINUX_CAP_SYS_ADMIN = 21
LINUX_NS_GET_PARENT = 0xB702
_owner_lock = threading.Lock()
_process_owner: "ProcessOwner | None" = None


class ProcessOwnerError(RuntimeError):
    """The local kernel boundary could not authoritatively identify a process."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ProcessBoundary:
    system: str
    host_id: str
    boot_id: str
    pid_namespace: str


@dataclass(frozen=True)
class ProcessSnapshot:
    start_id: str
    state: str


@dataclass(frozen=True)
class ProcessOwner:
    schema_version: int
    generation: str
    system: str
    host_id: str
    boot_id: str
    pid_namespace: str
    pid: int
    start_id: str

    def to_storage(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "system": self.system,
            "host_id": self.host_id,
            "boot_id": self.boot_id,
            "pid_namespace": self.pid_namespace,
            "pid": self.pid,
            "start_id": self.start_id,
        }

    @classmethod
    def from_storage(cls, value: Any) -> "ProcessOwner":
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "generation",
            "system",
            "host_id",
            "boot_id",
            "pid_namespace",
            "pid",
            "start_id",
        }:
            raise ProcessOwnerError("account_writer_owner_corrupt")
        owner = cls(
            schema_version=value["schema_version"],
            generation=value["generation"],
            system=value["system"],
            host_id=value["host_id"],
            boot_id=value["boot_id"],
            pid_namespace=value["pid_namespace"],
            pid=value["pid"],
            start_id=value["start_id"],
        )
        if (
            not isinstance(owner.schema_version, int)
            or isinstance(owner.schema_version, bool)
            or owner.schema_version != OWNER_SCHEMA_VERSION
            or not isinstance(owner.generation, str)
            or len(owner.generation) != 32
            or not all(character in "0123456789abcdef" for character in owner.generation)
            or not isinstance(owner.system, str)
            or owner.system not in SUPPORTED_RECOVERY_SYSTEMS
            or not isinstance(owner.host_id, str)
            or len(owner.host_id) != 64
            or not all(character in "0123456789abcdef" for character in owner.host_id)
            or not isinstance(owner.boot_id, str)
            or not owner.boot_id
            or not isinstance(owner.pid_namespace, str)
            or not owner.pid_namespace
            or not isinstance(owner.pid, int)
            or isinstance(owner.pid, bool)
            or owner.pid <= 0
            or not isinstance(owner.start_id, str)
            or not owner.start_id
        ):
            raise ProcessOwnerError("account_writer_owner_corrupt")
        return owner

    def fingerprint(self) -> str:
        encoded = json.dumps(self.to_storage(), sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def _read_required_text(path: Path, *, code: str) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError) as exc:
        raise ProcessOwnerError(code) from exc
    if not value:
        raise ProcessOwnerError(code)
    return value


def _linux_boundary() -> ProcessBoundary:
    boot_id = _read_required_text(Path("/proc/sys/kernel/random/boot_id"), code="account_writer_boot_unknown")
    try:
        pid_namespace = os.readlink("/proc/self/ns/pid")
    except OSError as exc:
        raise ProcessOwnerError("account_writer_pid_namespace_unknown") from exc
    host_material = f"linux-host-boot:{boot_id}"
    return ProcessBoundary(
        system="Linux",
        host_id=hashlib.sha256(host_material.encode("ascii")).hexdigest(),
        boot_id=boot_id,
        pid_namespace=pid_namespace,
    )


def _darwin_boundary() -> ProcessBoundary:
    try:
        completed = subprocess.run(
            ["sysctl", "-n", "kern.boottime"],
            check=True,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessOwnerError("account_writer_boot_unknown") from exc
    boot_id = completed.stdout.strip()
    hostname = socket.gethostname().strip()
    if not boot_id or not hostname:
        raise ProcessOwnerError("account_writer_host_unknown")
    return ProcessBoundary(
        system="Darwin",
        host_id=hashlib.sha256(hostname.encode("utf-8")).hexdigest(),
        boot_id=boot_id,
        pid_namespace="darwin-host-process-table",
    )


def current_process_boundary() -> ProcessBoundary:
    system = platform.system()
    if system == "Linux":
        return _linux_boundary()
    if system == "Darwin":
        return _darwin_boundary()
    raise ProcessOwnerError("account_writer_os_boundary_unsupported")


def _linux_process_snapshot(pid: int) -> ProcessSnapshot | None:
    return _linux_process_snapshot_path(Path(f"/proc/{pid}/stat"))


def _linux_process_snapshot_path(path: Path) -> ProcessSnapshot | None:
    try:
        raw = path.read_text(encoding="ascii")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeError) as exc:
        raise ProcessOwnerError("account_writer_process_state_unknown") from exc
    closing_parenthesis = raw.rfind(")")
    fields = raw[closing_parenthesis + 2 :].split() if closing_parenthesis >= 0 else []
    if len(fields) <= 19:
        raise ProcessOwnerError("account_writer_process_state_unknown")
    return ProcessSnapshot(start_id=f"linux-proc-start:{fields[19]}", state=fields[0])


def _linux_has_supervisor_authority() -> bool:
    try:
        status = Path("/proc/self/status").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise ProcessOwnerError("account_writer_supervisor_authority_unknown") from exc
    cap_eff = next((line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("CapEff:")), None)
    if cap_eff is None:
        raise ProcessOwnerError("account_writer_supervisor_authority_unknown")
    try:
        capabilities = int(cap_eff, 16)
    except ValueError as exc:
        raise ProcessOwnerError("account_writer_supervisor_authority_unknown") from exc
    if not capabilities & (1 << LINUX_CAP_SYS_ADMIN):
        return False
    try:
        namespace_fd = os.open("/proc/self/ns/pid", os.O_RDONLY)
    except OSError as exc:
        raise ProcessOwnerError("account_writer_supervisor_authority_unknown") from exc
    try:
        try:
            parent_fd = fcntl.ioctl(namespace_fd, LINUX_NS_GET_PARENT)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                return True
            if exc.errno in {errno.EPERM, errno.EACCES}:
                return False
            raise ProcessOwnerError("account_writer_supervisor_authority_unknown") from exc
        else:
            os.close(parent_fd)
            return False
    finally:
        os.close(namespace_fd)


def linux_supervisor_process_snapshot(pid_namespace: str, namespace_pid: int) -> tuple[ProcessSnapshot | None, bool]:
    """Read an exact container PID from a host-supervisor process table."""
    if platform.system() != "Linux" or not _linux_has_supervisor_authority():
        raise ProcessOwnerError("account_writer_supervisor_authority_required")
    namespace_seen = False
    try:
        process_paths = tuple(Path("/proc").iterdir())
    except OSError as exc:
        raise ProcessOwnerError("account_writer_process_state_unknown") from exc
    for process_path in process_paths:
        if not process_path.name.isdigit():
            continue
        try:
            visible_namespace = os.readlink(process_path / "ns" / "pid")
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ProcessOwnerError("account_writer_process_state_unknown") from exc
        if visible_namespace != pid_namespace:
            continue
        namespace_seen = True
        try:
            status = (process_path / "status").read_text(encoding="ascii")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError) as exc:
            raise ProcessOwnerError("account_writer_process_state_unknown") from exc
        nspid = next((line.split(":", 1)[1].split() for line in status.splitlines() if line.startswith("NSpid:")), None)
        if nspid is None:
            raise ProcessOwnerError("account_writer_process_state_unknown")
        try:
            visible_pid = int(nspid[-1])
        except (IndexError, ValueError) as exc:
            raise ProcessOwnerError("account_writer_process_state_unknown") from exc
        if visible_pid != namespace_pid:
            continue
        return _linux_process_snapshot_path(process_path / "stat"), namespace_seen
    return None, namespace_seen


def _darwin_ps_value(pid: int, field: str) -> str | None:
    try:
        completed = subprocess.run(
            ["ps", "-o", f"{field}=", "-p", str(pid)],
            check=False,
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ProcessOwnerError("account_writer_process_state_unknown") from exc
    if completed.returncode != 0:
        return None
    value = completed.stdout.strip()
    return value or None


def _darwin_process_snapshot(pid: int) -> ProcessSnapshot | None:
    first_start = _darwin_ps_value(pid, "lstart")
    if first_start is None:
        return None
    state = _darwin_ps_value(pid, "state")
    second_start = _darwin_ps_value(pid, "lstart")
    if state is None or second_start is None or first_start != second_start:
        raise ProcessOwnerError("account_writer_process_state_unknown")
    return ProcessSnapshot(start_id=f"darwin-ps-start:{first_start}", state=state[:1])


def process_snapshot(system: str, pid: int) -> ProcessSnapshot | None:
    if system == "Linux":
        return _linux_process_snapshot(pid)
    if system == "Darwin":
        return _darwin_process_snapshot(pid)
    raise ProcessOwnerError("account_writer_os_boundary_unsupported")


def current_process_owner() -> ProcessOwner:
    """Return one immutable generation, regenerating safely after ``fork``."""
    global _process_owner
    pid = os.getpid()
    with _owner_lock:
        if _process_owner is not None and _process_owner.pid == pid:
            return _process_owner
        boundary = current_process_boundary()
        snapshot = process_snapshot(boundary.system, pid)
        if snapshot is None or snapshot.state == "Z":
            raise ProcessOwnerError("account_writer_process_state_unknown")
        _process_owner = ProcessOwner(
            schema_version=OWNER_SCHEMA_VERSION,
            generation=uuid.uuid4().hex,
            system=boundary.system,
            host_id=boundary.host_id,
            boot_id=boundary.boot_id,
            pid_namespace=boundary.pid_namespace,
            pid=pid,
            start_id=snapshot.start_id,
        )
        return _process_owner
