#!/opt/omi-recovery/venv/bin/python3 -I
"""Recover one orphaned writer from the pinned host-supervisor boundary."""

# This file is intentionally a production-only bootstrap. Keep the first stage
# limited to frozen/builtin modules: an untrusted cwd or PYTHONPATH must never
# run application or credential code before the kernel authority check.
import os
import sys

ROOT_UID = 0
LINUX_CAP_SYS_ADMIN = 21
LINUX_NS_GET_PARENT = 0xB702
RECOVERY_RELEASE_ROOT = "/opt/omi-recovery/backend"
RECOVERY_SCRIPT_PATH = RECOVERY_RELEASE_ROOT + "/scripts/content_writer_recovery.py"


def _abort_before_import(code: int) -> None:
    os._exit(code)


if sys.platform != "linux":
    _abort_before_import(78)
if not (sys.flags.isolated and sys.flags.safe_path and sys.flags.no_user_site and sys.flags.ignore_environment):
    _abort_before_import(77)
if os.geteuid() != ROOT_UID:
    _abort_before_import(77)

# Isolation is now active, so these standard-library imports cannot be
# substituted through cwd, PYTHONPATH, or user site-packages.
import errno
import fcntl
import stat
from pathlib import Path


def _read_kernel_file(path: str, *, maximum: int) -> bytes:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        payload = os.read(descriptor, maximum + 1)
    finally:
        os.close(descriptor)
    if len(payload) > maximum:
        _abort_before_import(77)
    return payload


def _require_initial_host_supervisor() -> None:
    try:
        status = _read_kernel_file("/proc/self/status", maximum=128 * 1024).decode("ascii")
        cap_eff = next(line.split(":", 1)[1].strip() for line in status.splitlines() if line.startswith("CapEff:"))
        capabilities = int(cap_eff, 16)
    except (OSError, UnicodeError, StopIteration, ValueError):
        _abort_before_import(77)
    if not capabilities & (1 << LINUX_CAP_SYS_ADMIN):
        _abort_before_import(77)
    try:
        namespace_fd = os.open(
            "/proc/self/ns/pid",
            os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
    except OSError:
        _abort_before_import(77)
    try:
        try:
            parent_fd = fcntl.ioctl(namespace_fd, LINUX_NS_GET_PARENT)
        except OSError as exc:
            if exc.errno == errno.EINVAL:
                return
            _abort_before_import(77)
        else:
            os.close(parent_fd)
            _abort_before_import(77)
    finally:
        os.close(namespace_fd)


_require_initial_host_supervisor()


def _require_root_owned_chain(path: Path, *, regular: bool, reject_symlink: bool) -> Path:
    if not path.is_absolute():
        _abort_before_import(77)
    current = path
    while True:
        try:
            metadata = current.lstat()
        except OSError:
            _abort_before_import(77)
        if metadata.st_uid != ROOT_UID or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
            _abort_before_import(77)
        if current == path:
            if reject_symlink and stat.S_ISLNK(metadata.st_mode):
                _abort_before_import(77)
            if regular and not (stat.S_ISREG(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode)):
                _abort_before_import(77)
            if not regular and not stat.S_ISDIR(metadata.st_mode):
                _abort_before_import(77)
        elif not stat.S_ISDIR(metadata.st_mode):
            _abort_before_import(77)
        if current == current.parent:
            break
        current = current.parent
    resolved = path.resolve(strict=True)
    if resolved != path:
        return _require_root_owned_chain(resolved, regular=regular, reject_symlink=True)
    return resolved


script_path = Path(__file__).absolute()
if str(script_path) != RECOVERY_SCRIPT_PATH:
    _abort_before_import(77)
_require_root_owned_chain(script_path, regular=True, reject_symlink=True)
release_root = _require_root_owned_chain(Path(RECOVERY_RELEASE_ROOT), regular=False, reject_symlink=True)
_require_root_owned_chain(Path(sys.executable), regular=True, reject_symlink=False)
if os.environ.get("FIRESTORE_EMULATOR_HOST") is not None:
    os.write(
        1,
        b'{"action":"content_writer_recovery","content_free":true,"reason":"account_writer_recovery_emulator_forbidden","result":"refused"}\n',
    )
    sys.exit(2)

# Application and Google modules become reachable only after isolation, the
# initial-host supervisor capability, and the root-owned release are proven.
sys.path.insert(0, str(release_root))

import argparse
import json
from typing import Any, Sequence

from database import content_write_recovery, content_write_recovery_authority


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog=(
            "Run only from the installed initial-host supervisor release. "
            "A refusal retains DRAINING and authorizes no manual release."
        ),
    )
    parser.add_argument(
        "--subject-hash",
        required=True,
        help="SHA-256 of the exact Firebase UID; plaintext UIDs are not accepted",
    )
    parser.add_argument(
        "--token-hash",
        required=True,
        help="SHA-256 of the exact durable writer token; plaintext tokens are not accepted",
    )
    return parser


def _print_receipt(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    refusal = {
        "action": "content_writer_recovery",
        "content_free": True,
        "result": "refused",
    }
    try:
        (
            firestore_db,
            authority,
            transactional,
        ) = content_write_recovery_authority.load_production_recovery_firestore_client()
        del authority
        receipt = content_write_recovery.recover_orphaned_writer(
            firestore_db,
            subject_hash=args.subject_hash,
            token_hash=args.token_hash,
            transactional=transactional,
        )
    except (
        content_write_recovery.ContentWriterRecoveryError,
        content_write_recovery_authority.RecoveryAuthorityError,
    ) as exc:
        _print_receipt({**refusal, "reason": exc.code})
        return 2
    _print_receipt(receipt.to_dict())
    return 0


if __name__ == "__main__":
    sys.exit(main())
