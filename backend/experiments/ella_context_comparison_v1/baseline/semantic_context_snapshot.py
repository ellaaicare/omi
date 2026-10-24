"""Internal exact-owner durable workspace hydration; never a request-body import.

Runtime must already have passed the existing repository's authority resolver.
This helper reads only its approved workspace. No search, remote fallback,
truncation, symlink traversal, or private-content logging is performed.
"""

from __future__ import annotations
import hashlib
import json
import os
from pathlib import PurePosixPath
import stat


class CompanionContextUnavailable(ValueError):
    pass


SOURCES = {
    'HERMES-CONTEXT.md': ('HERMES-CONTEXT.md',),
    'USER.md': ('USER.md',),
    'MEMORY.md': ('MEMORY.md',),
    'environment': ('ENVIRONMENT.md', 'environment.md'),
    'TIMELINE.md': ('TIMELINE.md',),
    'scannercontext': ('scanner-tuning.md',),
    'recentOMIindex': ('recent-conversations.md',),
}
MAX_DOCUMENT_BYTES = 65536
MAX_SNAPSHOT_BYTES = 131072


def _digest(value):
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    ).hexdigest()


def _open_root(path):
    parts = PurePosixPath(path).parts
    if not parts or parts[0] != '/' or any(p in {'.', '..'} for p in parts):
        raise CompanionContextUnavailable('companion_workspace_invalid')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:]:
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = nxt
        if os.fstat(fd).st_mode & 0o022:
            raise CompanionContextUnavailable('companion_workspace_writable_by_others')
        return fd
    except BaseException:
        os.close(fd)
        raise


def _document(root, name):
    try:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=root)
    except FileNotFoundError:
        return None
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_mode & 0o022
            or before.st_size > MAX_DOCUMENT_BYTES
        ):
            raise CompanionContextUnavailable('companion_document_invalid')
        pieces = []
        total = 0
        while True:
            piece = os.read(fd, min(8192, MAX_DOCUMENT_BYTES + 1 - total))
            if not piece:
                break
            total += len(piece)
            pieces.append(piece)
            if total > MAX_DOCUMENT_BYTES:
                raise CompanionContextUnavailable('companion_document_capacity')
        after = os.fstat(fd)
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        if signature(before) != signature(after):
            raise CompanionContextUnavailable('companion_document_changed')
        return b''.join(pieces).decode('utf-8', errors='strict')
    finally:
        os.close(fd)


def hydrate_owned_workspace(runtime, *, uid, runtime_identity=None):
    """Build a full immutable snapshot from an already-resolved self-hosted runtime.

    Missing documents become explicit empty entries; missing workspace holds.
    Cloud targets have no local workspace and cannot silently borrow one.
    Caller revalidates runtime/consent before and after hydration and dispatch.
    Revision binds approved owner profile identity plus every retained document.
    """
    if (
        not uid
        or runtime.uid != uid
        or runtime.provider != 'hermes'
        or not runtime.binding_id
        or not runtime.profile_name
        or not runtime.workspace_root
    ):
        raise CompanionContextUnavailable('companion_owner_runtime_required')
    # This secret-free frozen identity is supplied by the existing authority
    # resolver. Do not derive a partial identity from workspace/profile fields:
    # endpoint, model, consent and target lineage changes must stale proposals.
    authority_digest = getattr(runtime_identity, 'digest', None)
    authority_mode = getattr(runtime_identity, 'target_mode', None)
    if (
        getattr(runtime_identity, 'uid', None) != uid
        or not isinstance(authority_mode, str)
        or not authority_mode
        or not isinstance(authority_digest, str)
        or len(authority_digest) != 64
        or any(c not in '0123456789abcdef' for c in authority_digest)
    ):
        raise CompanionContextUnavailable('companion_runtime_authority_required')
    authority = {'uid': uid, 'target_mode': authority_mode, 'digest': authority_digest}
    try:
        root = _open_root(runtime.workspace_root)
        try:
            directory_signature = lambda s: (
                s.st_dev,
                s.st_ino,
                s.st_mode,
                s.st_uid,
                s.st_gid,
                s.st_mtime_ns,
                s.st_ctime_ns,
            )
            initial_directory = directory_signature(os.fstat(root))

            def read_all():
                return {name: _document(root, name) for names in SOURCES.values() for name in names}

            original = read_all()
            if read_all() != original:
                raise CompanionContextUnavailable('companion_snapshot_changed')
            # A pinned fd alone survives rename/removal. Confirm the approved
            # pathname still resolves to this same directory, and reject any
            # directory-entry or permission changes during collection.
            current_root = _open_root(runtime.workspace_root)
            try:
                if (
                    directory_signature(os.fstat(root)) != initial_directory
                    or directory_signature(os.fstat(current_root)) != initial_directory
                ):
                    raise CompanionContextUnavailable('companion_workspace_changed')
            finally:
                os.close(current_root)
        finally:
            os.close(root)
        documents = {}
        for key, names in SOURCES.items():
            present = [(name, original[name]) for name in names if original[name] is not None]
            documents[key] = (
                present[0][1]
                if len(present) == 1
                else '\n\n'.join('## ' + name + '\n' + text for name, text in present)
            )
        revision = _digest(
            {
                'runtime_authority': authority,
                'binding_id': runtime.binding_id,
                'runtime_revision': runtime.revision,
                'profile': runtime.profile_name,
                'documents': original,
            }
        )
        snapshot = {'owner_uid': uid, 'revision': revision, 'documents': documents}
        if len(json.dumps(snapshot, ensure_ascii=False).encode()) > MAX_SNAPSHOT_BYTES:
            raise CompanionContextUnavailable('companion_snapshot_capacity')
        return snapshot
    except (OSError, UnicodeError) as exc:
        raise CompanionContextUnavailable('companion_workspace_unavailable') from exc
