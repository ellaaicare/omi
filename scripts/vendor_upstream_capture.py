#!/usr/bin/env python3
"""(Re)vendor the upstream capture stack from BasedHardware/omi at a pinned commit.

Usage:
    git fetch https://github.com/BasedHardware/omi.git <pin>
    python3 scripts/vendor_upstream_capture.py --pin <pin> --list scripts/upstream_capture_files.txt

The list file names UPSTREAM paths (one per line, '#' comments allowed). Each
file is copied byte-for-byte from the pin into its relocated local path
(``app/lib/<rel>`` -> ``app/lib/upstream_capture/<rel>``; everything else keeps
its upstream path), then Dart files get exactly one mechanical rewrite:
``'package:omi/<rel>'`` -> ``'package:omi/upstream_capture/<rel>'`` for every
vendored ``<rel>``. Nothing else is touched. The manifest
``app/lib/upstream_capture/UPSTREAM_OWNED.txt`` is regenerated with the upstream
blob id of every file so ``verify_upstream_capture_identity.py`` can check
identity offline.
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from verify_upstream_capture_identity import (  # noqa: E402
    MANIFEST,
    REPO_ROOT,
    UPSTREAM_DART_ROOT,
    kind_for,
    local_path_for,
    pin_blob,
    relocate,
)

HEADER = """# Upstream-owned files for the Ella upstream capture port (ellaaicare/ella-ai#1280).
# Source: https://github.com/BasedHardware/omi at the pin below. DO NOT EDIT vendored files.
# The only permitted change is the Dart import relocation
#   'package:omi/<rel>' -> 'package:omi/upstream_capture/<rel>'  (only for vendored <rel>)
# Verified by scripts/verify_upstream_capture_identity.py (app/test/upstream_capture/byte_identity_guard_test.dart).
# Regenerate with scripts/vendor_upstream_capture.py --pin <pin> --list scripts/upstream_capture_files.txt
# Kinds: dart-relocated (namespaced copy), verbatim (native/doc at upstream path),
#        in-place (shared fork file replaced by the pin's exact bytes at its original path).
# Columns (tab separated): kind, upstream git blob id at pin, upstream path, local path
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--pin', required=True)
    parser.add_argument('--list', required=True)
    args = parser.parse_args()

    upstream_paths: list[str] = []
    in_place: set[str] = set()
    with open(args.list, encoding='utf-8') as fh:
        for raw in fh:
            line = raw.split('#', 1)[0].strip()
            if line.startswith('inplace '):
                line = line[len('inplace ') :].strip()
                in_place.add(line)
            if line and line not in upstream_paths:
                upstream_paths.append(line)
    upstream_paths.sort()

    rels = {
        p[len(UPSTREAM_DART_ROOT) :]
        for p in upstream_paths
        if p.endswith('.dart') and p.startswith(UPSTREAM_DART_ROOT) and p not in in_place
    }
    rows: list[str] = []
    for upstream_path in upstream_paths:
        blob, content = pin_blob(args.pin, upstream_path)
        if blob is None or content is None:
            print(f'missing at pin: {upstream_path}', file=sys.stderr)
            return 1
        kind = kind_for(upstream_path, upstream_path in in_place)
        local = local_path_for(upstream_path, kind)
        if kind == 'dart-relocated':
            text = content.decode('utf-8')
            if 'package:omi/upstream_capture/' in text:
                print(f'upstream file already names upstream_capture: {upstream_path}', file=sys.stderr)
                return 1
            content = relocate(text, rels).encode('utf-8')
        dest = os.path.join(REPO_ROOT, local)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, 'wb') as out:
            out.write(content)
        rows.append('\t'.join((kind, blob, upstream_path, local)))

    with open(os.path.join(REPO_ROOT, MANIFEST), 'w', encoding='utf-8') as out:
        out.write(HEADER)
        out.write(f'pin {args.pin}\n')
        for row in rows:
            out.write(row + '\n')
    print(f'vendored {len(rows)} files from {args.pin}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
