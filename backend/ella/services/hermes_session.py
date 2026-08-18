"""Shared Hermes/Honcho session key helpers."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from dataclasses import dataclass

CANONICAL_OWNER_KEY_VERSION = "v2"
CANONICAL_OWNER_KEY_DOMAIN = b"ella:omi:canonical-owner:v2\x00"


@dataclass(frozen=True)
class CanonicalOmiSessionKeyMigration:
    """Byte-exact keys for an owner-verified, peer-preserving session rebind."""

    legacy_key: str
    v2_key: str


def safe_session_component(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.:-]+", "-", str(value or "")).strip("-")[:160] or "unknown"


def canonical_owner_component(uid: str) -> str:
    """Digest an authority-validated canonical Firebase UID without normalization."""

    if not isinstance(uid, str) or not uid or uid != uid.strip():
        raise ValueError("canonical owner uid must be a non-empty string without surrounding whitespace")
    owner_digest = hashlib.sha256(CANONICAL_OWNER_KEY_DOMAIN + uid.encode("utf-8")).hexdigest()
    return f"{CANONICAL_OWNER_KEY_VERSION}-{owner_digest}"


def canonical_omi_session_key(uid: str) -> str:
    """Stable Hermes/Honcho scope; legacy continuity requires an owner-verified migration."""

    return f"ella:omi:{canonical_owner_component(uid)}:canonical"


def canonical_omi_session_key_migration(uid: str) -> CanonicalOmiSessionKeyMigration:
    """Describe an offline migration without allowing request-path legacy fallback.

    The source helper emits keys only and never reads provider content. The operator
    must prove the exact owner, precreate v2 in the same owner-verified workspace,
    bind it to the same verified peer identities, and read back the association plus
    equivalent peer-scoped context/representation hash and count facts before an
    atomic all-proxy cutover. Raw message copying is not required. Keeping the legacy
    session unchanged makes rollback reversible.
    """

    v2_key = canonical_omi_session_key(uid)
    legacy_component = safe_session_component(uid.lower())
    return CanonicalOmiSessionKeyMigration(
        legacy_key=f"ella:omi:{legacy_component}:canonical",
        v2_key=v2_key,
    )


def preflight_canonical_omi_session_key_migration(
    uid: str, retained_legacy_keys: Iterable[str]
) -> CanonicalOmiSessionKeyMigration:
    """Fail closed unless the v2 target is disjoint from every retained legacy key."""

    migration = canonical_omi_session_key_migration(uid)
    if isinstance(retained_legacy_keys, (str, bytes)):
        raise ValueError("retained legacy keys must be an iterable of non-empty strings")
    retained_keys = tuple(retained_legacy_keys)
    if any(not isinstance(key, str) or not key for key in retained_keys):
        raise ValueError("retained legacy keys must be an iterable of non-empty strings")
    if migration.v2_key in retained_keys:
        raise ValueError("canonical v2 target collides with a retained legacy key")
    return migration
