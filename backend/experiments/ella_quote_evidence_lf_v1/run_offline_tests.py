"""Materialize only hash-pinned public source fixtures; no credentials/network/SDK."""

import hashlib, json, os, subprocess, sys, tempfile
from pathlib import Path

root = Path(__file__).resolve().parent
manifest = json.loads((root / 'SOURCE.MANIFEST.json').read_text())
with tempfile.TemporaryDirectory(prefix='quote-lf-pure-', dir=root) as folder:
    target = Path(folder)
    for name, pin in manifest['pins'].items():
        path = root / name
        if path.is_symlink() or not path.is_file():
            raise SystemExit('unsafe fixture source')
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != pin:
            raise SystemExit('fixture source pin changed')
        destination = target / name.removesuffix('.source')
        destination.parent.mkdir(parents=True, exist_ok=True)
        if name == 'tests/test_quote_evidence.py.source':
            original = b"BASE=W/'repo/backend/serving_snapshots/unified-canonical-memory-4235154f/backend'"
            if data.count(original) != 1:
                raise SystemExit('fixture layout changed')
            data = data.replace(
                original, b"BASE=ROOT/'baseline'"
            )  # Path-only fixture rebase; all tests/product bytes unchanged.
        destination.write_bytes(data)
    env = os.environ.copy()
    env['PYTHONDONTWRITEBYTECODE'] = '1'
    for suite in ['tests', 'reviewer-tests']:
        result = subprocess.run(
            [sys.executable, '-B', '-m', 'unittest', 'discover', '-s', str(target / suite), '-p', 'test_*.py'],
            cwd=target,
            env=env,
            timeout=30,
        )
        if result.returncode:
            raise SystemExit(result.returncode)
