#!/usr/bin/env python3
"""Byte-identity guard for the vendored upstream capture stack (ellaaicare/ella-ai#1280).

Every file listed in ``app/lib/upstream_capture/UPSTREAM_OWNED.txt`` is a copy of
``BasedHardware/omi`` at the pinned commit. The ONLY permitted difference is the
mechanical import-path relocation applied to Dart files:

    'package:omi/<rel>'  ->  'package:omi/upstream_capture/<rel>'

and only for ``<rel>`` values that are themselves vendored (listed in the
manifest). Swift / Objective-C / Markdown files must be byte-identical.

Checks (all must pass, exit status 1 otherwise):

1. Offline, from a clean checkout: every listed local file exists; undoing the
   relocation yields content whose git blob id equals the upstream blob id that
   the manifest records for that path at the pin. Any change other than the
   relocation (a patched line, a reformatting, an extra import pointing into
   ``upstream_capture``) changes the blob id and fails.
2. No unlisted file lives under ``app/lib/upstream_capture/`` (Ella adapter code
   must live outside the vendored tree), except the manifest/patch docs.
3. Online (when the pin commit object is available locally, e.g. after
   ``git fetch https://github.com/BasedHardware/omi.git <pin>``): the recorded
   blob ids equal ``git ls-tree <pin>``, and the forward relocation of the real
   upstream bytes equals the vendored bytes; mismatches print a unified diff.
   Pass ``--require-pin`` to make the pin's absence an error.

A file patched relative to the pin is declared in the manifest with kind
``patched`` (see UPSTREAM_PATCHES.md for the list and rationale). A ``patched``
entry is placed exactly like ``dart-relocated`` (so its own imports and any
importer's relocated import of it still resolve). Its manifest row records both
the upstream pin blob and the exact approved local blob. The guard checks both,
so an upstream change flags the patch as stale and any unrecorded local change
fails closed.
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MANIFEST = os.path.join('app', 'lib', 'upstream_capture', 'UPSTREAM_OWNED.txt')
VENDOR_ROOT = os.path.join('app', 'lib', 'upstream_capture')
# Upstream's own capture tests/fixtures, vendored to run against the vendored stack.
VENDOR_TEST_ROOT = os.path.join('app', 'test', 'upstream_capture')
ALLOWED_UNLISTED = {'UPSTREAM_OWNED.txt', 'UPSTREAM_PATCHES.md', 'README.md'}
PACKAGE_PREFIX = 'package:omi/'
RELOCATED_PREFIX = 'package:omi/upstream_capture/'
UPSTREAM_DART_ROOT = 'app/lib/'
LOCAL_DART_ROOT = 'app/lib/upstream_capture/'
UPSTREAM_TEST_ROOT = 'app/test/'
LOCAL_TEST_ROOT = 'app/test/upstream_capture/'
# dart-relocated: Dart file under app/lib/upstream_capture/ with the import relocation applied.
# verbatim:       non-Dart file (Swift/ObjC/Markdown) at its upstream path, byte-identical.
# in-place:       shared fork Dart file replaced by the pin's bytes at its ORIGINAL path (a strict,
#                 compatible superset of the fork copy), byte-identical and NOT relocated.
# patched:        Dart file under app/lib/upstream_capture/, relocated like dart-relocated, but
#                 deliberately NOT byte-identical to the pin. Documented in UPSTREAM_PATCHES.md.
KINDS = ('dart-relocated', 'verbatim', 'in-place', 'patched')

_QUOTED_PACKAGE_URI = re.compile(r"""(['"])package:omi/([^'"\s]+)\1""")


@dataclass(frozen=True)
class Entry:
    kind: str
    blob: str
    upstream_path: str
    local_path: str
    local_blob: str | None = None


def git_blob_id(data: bytes) -> str:
    return hashlib.sha1(b'blob %d\0' % len(data) + data).hexdigest()


def local_path_for(upstream_path: str, kind: str | None = None) -> str:
    """Deterministic placement rule shared by the vendoring tool and the guard."""
    if kind == 'in-place':
        return upstream_path
    if upstream_path.startswith(UPSTREAM_DART_ROOT):
        return LOCAL_DART_ROOT + upstream_path[len(UPSTREAM_DART_ROOT) :]
    if upstream_path.startswith(UPSTREAM_TEST_ROOT):
        return LOCAL_TEST_ROOT + upstream_path[len(UPSTREAM_TEST_ROOT) :]
    return upstream_path


def kind_for(upstream_path: str, in_place: bool = False) -> str:
    if in_place:
        return 'in-place'
    return 'dart-relocated' if upstream_path.endswith('.dart') else 'verbatim'


def relocate(source: str, vendored_rels: set[str]) -> str:
    """Forward mechanical rewrite: point imports of vendored files at the namespaced copy."""

    def sub(match: re.Match[str]) -> str:
        quote, rel = match.group(1), match.group(2)
        if rel in vendored_rels:
            return f'{quote}{RELOCATED_PREFIX}{rel}{quote}'
        return match.group(0)

    return _QUOTED_PACKAGE_URI.sub(sub, source)


def unrelocate(source: str, vendored_rels: set[str]) -> str:
    """Inverse of :func:`relocate` (exact because upstream never names upstream_capture)."""

    def sub(match: re.Match[str]) -> str:
        quote, rel = match.group(1), match.group(2)
        prefix = 'upstream_capture/'
        if rel.startswith(prefix) and rel[len(prefix) :] in vendored_rels:
            return f'{quote}{PACKAGE_PREFIX}{rel[len(prefix):]}{quote}'
        return match.group(0)

    return _QUOTED_PACKAGE_URI.sub(sub, source)


def read_manifest(path: str) -> tuple[str, list[Entry]]:
    pin = ''
    entries: list[Entry] = []
    with open(path, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.rstrip('\n')
            if not line or line.startswith('#'):
                continue
            if line.startswith('pin '):
                pin = line.split()[1]
                continue
            parts = line.split('\t')
            if len(parts) not in (4, 5):
                raise SystemExit(f'Malformed manifest line: {line!r}')
            entry = Entry(*parts)
            if entry.kind == 'patched' and not re.fullmatch(r'[0-9a-f]{40}', entry.local_blob or ''):
                raise SystemExit(f'Patched manifest line has no approved 40-hex local blob: {line!r}')
            if entry.kind != 'patched' and entry.local_blob is not None:
                raise SystemExit(f'Only patched manifest lines may record a local blob: {line!r}')
            entries.append(entry)
    if not re.fullmatch(r'[0-9a-f]{40}', pin):
        raise SystemExit('Manifest has no 40-hex pin line')
    return pin, entries


def vendored_dart_rels(entries: list[Entry]) -> set[str]:
    return {
        e.upstream_path[len(UPSTREAM_DART_ROOT) :]
        for e in entries
        if e.kind in ('dart-relocated', 'patched') and e.upstream_path.startswith(UPSTREAM_DART_ROOT)
    }


def git(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(['git', '-C', REPO_ROOT, *args], capture_output=True)


def pin_available(pin: str) -> bool:
    return git('cat-file', '-e', f'{pin}^{{commit}}').returncode == 0


def pin_blob(pin: str, upstream_path: str) -> tuple[str | None, bytes | None]:
    listing = git('ls-tree', pin, '--', upstream_path)
    if listing.returncode != 0 or not listing.stdout.strip():
        return None, None
    blob = listing.stdout.decode().split()[2]
    content = git('cat-file', 'blob', blob)
    return blob, content.stdout if content.returncode == 0 else None


def verify(require_pin: bool, verbose: bool) -> int:
    manifest_path = os.path.join(REPO_ROOT, MANIFEST)
    pin, entries = read_manifest(manifest_path)
    rels = vendored_dart_rels(entries)
    failures: list[str] = []

    seen_local: set[str] = set()
    for e in entries:
        if e.kind not in KINDS:
            failures.append(f'{e.local_path}: kind {e.kind!r} is not one of {KINDS}')
            continue
        if e.local_path != local_path_for(e.upstream_path, e.kind):
            failures.append(f'{e.local_path}: placement does not follow the relocation rule for {e.upstream_path}')
        # 'patched' is placed and relocated exactly like 'dart-relocated'; only its
        # byte-identity assertion (below) differs.
        structural_kind = 'dart-relocated' if e.kind == 'patched' else e.kind
        if structural_kind != kind_for(e.upstream_path, structural_kind == 'in-place') or (
            structural_kind == 'in-place' and not e.upstream_path.endswith('.dart')
        ):
            failures.append(f'{e.local_path}: kind {e.kind} does not match file type')
        if e.local_path in seen_local:
            failures.append(f'{e.local_path}: listed twice')
        seen_local.add(e.local_path)
        path = os.path.join(REPO_ROOT, e.local_path)
        if not os.path.isfile(path):
            failures.append(f'{e.local_path}: missing')
            continue
        data = open(path, 'rb').read()
        if e.kind == 'patched':
            local_blob = git_blob_id(data)
            if local_blob != e.local_blob:
                failures.append(f'{e.local_path}: local blob {local_blob} != approved patched blob {e.local_blob}')
        elif e.kind == 'dart-relocated':
            try:
                text = data.decode('utf-8')
            except UnicodeDecodeError:
                failures.append(f'{e.local_path}: not UTF-8')
                continue
            original = unrelocate(text, rels).encode('utf-8')
            if git_blob_id(original) != e.blob:
                failures.append(f'{e.local_path}: differs from upstream {e.upstream_path}@{pin[:12]} (blob {e.blob})')
        else:
            original = data
            if git_blob_id(original) != e.blob:
                failures.append(f'{e.local_path}: differs from upstream {e.upstream_path}@{pin[:12]} (blob {e.blob})')

    # No stray files inside the vendored Dart trees (Ella code must live outside them).
    for root in (VENDOR_ROOT, VENDOR_TEST_ROOT):
        for dirpath, _, files in os.walk(os.path.join(REPO_ROOT, root)):
            for name in files:
                rel = os.path.relpath(os.path.join(dirpath, name), REPO_ROOT).replace(os.sep, '/')
                if rel in seen_local:
                    continue
                if os.path.dirname(rel) == VENDOR_ROOT.replace(os.sep, '/') and name in ALLOWED_UNLISTED:
                    continue
                failures.append(f'{rel}: unlisted file inside the upstream-owned tree')

    online = pin_available(pin)
    if not online and require_pin:
        failures.append(f'pin {pin} is not available locally (fetch it from BasedHardware/omi)')
    if online:
        for e in entries:
            blob, upstream = pin_blob(pin, e.upstream_path)
            if blob is None or upstream is None:
                failures.append(f'{e.upstream_path}: not present at pin {pin}')
                continue
            if blob != e.blob:
                failures.append(f'{e.upstream_path}: manifest blob {e.blob} != pin blob {blob}')
            if e.kind == 'patched':
                # The upstream blob still has to track the pin so the patch does not
                # silently go stale. The exact local content was checked above.
                continue
            path = os.path.join(REPO_ROOT, e.local_path)
            if not os.path.isfile(path):
                continue
            expected = relocate(upstream.decode('utf-8'), rels).encode('utf-8') if e.kind == 'dart-relocated' else upstream
            actual = open(path, 'rb').read()
            if expected != actual:
                diff = difflib.unified_diff(
                    expected.decode('utf-8', 'replace').splitlines(keepends=True),
                    actual.decode('utf-8', 'replace').splitlines(keepends=True),
                    fromfile=f'expected:{e.local_path}',
                    tofile=f'actual:{e.local_path}',
                )
                failures.append(f'{e.local_path}: byte diff vs relocated upstream:\n' + ''.join(diff))

    mode = 'online (pin object present)' if online else 'offline (recorded blob ids)'
    if failures:
        print(f'UPSTREAM CAPTURE IDENTITY: FAIL [{mode}] {len(failures)} problem(s)')
        for failure in failures:
            print(' - ' + failure)
        return 1
    counts = {k: sum(1 for e in entries if e.kind == k) for k in KINDS}
    summary = ', '.join(f'{k}={v}' for k, v in counts.items())
    print(f'UPSTREAM CAPTURE IDENTITY: OK [{mode}] pin={pin} files={len(entries)} ({summary})')
    if verbose:
        for e in entries:
            print(f'  {e.kind:15} {e.local_path}')
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--require-pin', action='store_true', help='fail when the pin commit object is not available')
    parser.add_argument('-v', '--verbose', action='store_true')
    args = parser.parse_args()
    return verify(args.require_pin, args.verbose)


if __name__ == '__main__':
    sys.exit(main())
