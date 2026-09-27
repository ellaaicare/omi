#!/usr/bin/env python3
"""Byte-identity guard for vendored upstream capture-layer files.

Fetches the pinned commit from the upstream repository named in
UPSTREAM_CAPTURE_MANIFEST.json and diffs each manifested path, byte for byte,
against the copy checked into this repository. Exits non-zero on any
mismatch, missing local file, or missing upstream file — there is no
"close enough" outcome.

Usage:
    scripts/verify_upstream_capture_identity.py [manifest-path]

See UPSTREAM_PATCHES.md for the paths this guards and why.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REMOTE_NAME = "ella-upstream-capture-guard"


def repo_root() -> Path:
    out = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(out.stdout.strip())


def ensure_remote(root: Path, url: str) -> None:
    existing = subprocess.run(
        ["git", "-C", str(root), "remote", "get-url", REMOTE_NAME],
        capture_output=True,
        text=True,
    )
    if existing.returncode == 0:
        if existing.stdout.strip() != url:
            subprocess.run(
                ["git", "-C", str(root), "remote", "set-url", REMOTE_NAME, url],
                check=True,
            )
        return
    subprocess.run(
        ["git", "-C", str(root), "remote", "add", REMOTE_NAME, url],
        check=True,
    )


def fetch_pin(root: Path, sha: str) -> None:
    subprocess.run(
        ["git", "-C", str(root), "fetch", "--depth", "1", REMOTE_NAME, sha],
        check=True,
    )


def upstream_blob(root: Path, sha: str, path: str) -> bytes | None:
    result = subprocess.run(
        ["git", "-C", str(root), "show", f"{sha}:{path}"],
        capture_output=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout


def main() -> int:
    root = repo_root()
    manifest_path = Path(sys.argv[1]) if len(sys.argv) > 1 else root / "UPSTREAM_CAPTURE_MANIFEST.json"
    manifest = json.loads(manifest_path.read_text())

    upstream_repo = manifest["upstream_repo"]
    upstream_sha = manifest["upstream_sha"]
    paths = manifest["paths"]

    print(f"Upstream repo: {upstream_repo}")
    print(f"Pinned SHA:    {upstream_sha}")
    print(f"Guarded paths: {len(paths)}")
    print()

    ensure_remote(root, upstream_repo)
    fetch_pin(root, upstream_sha)

    failures: list[str] = []
    for rel_path in paths:
        upstream_bytes = upstream_blob(root, upstream_sha, rel_path)
        local_path = root / rel_path

        if upstream_bytes is None:
            failures.append(f"{rel_path}: not found in upstream at {upstream_sha}")
            print(f"FAIL  {rel_path} (missing upstream)")
            continue

        if not local_path.is_file():
            failures.append(f"{rel_path}: not found locally")
            print(f"FAIL  {rel_path} (missing locally)")
            continue

        local_bytes = local_path.read_bytes()
        if local_bytes != upstream_bytes:
            failures.append(f"{rel_path}: byte drift from upstream {upstream_sha}")
            print(f"FAIL  {rel_path} (byte drift)")
            continue

        print(f"OK    {rel_path}")

    print()
    if failures:
        print(f"{len(failures)} of {len(paths)} guarded path(s) drifted from upstream:")
        for failure in failures:
            print(f"  - {failure}")
        return 1

    print(f"All {len(paths)} guarded path(s) are byte-identical to {upstream_repo}@{upstream_sha}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
