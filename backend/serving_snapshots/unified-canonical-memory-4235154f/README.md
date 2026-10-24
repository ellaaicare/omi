# Serving-source evidence, captured October 24, 2026 UTC

This is an immutable, byte-preserved snapshot of 441 runtime Python files (6,414,728 bytes) read from the deployed release's backend directory. It is **not** a new deployment and is not imported by the fork's production package.

`SERVING.SOURCE.MANIFEST.json` records every path, size and SHA-256, static local import edges, external import roots and unresolved references. Source was reread before capture completed; a changed file would have aborted capture. Tests, scripts, environments, credentials, records and other data are excluded. Credential-literal checks flagged no included source file. No application module was imported or provider called during capture.

The known service PID was present and its working directory matched this source root. This records on-disk source, not an in-memory bytecode dump. All touched queue candidate baselines match this snapshot exactly.

The static map reports `modal` references unresolved: the source has a `modal/` directory while the installed third-party package may provide that import. This namespace collision is not evidence of a missing local module. It requires environment/import acceptance. Dynamic runtime configuration is intentionally not exported.

`SERVING.DEPENDENCIES.json` records the initial system-interpreter distribution inventory, explicitly not the service environment. `SERVING.INTERPRETER.DISTRIBUTIONS.json` records names and versions from the interpreter identified by the observed service's first executable argument. Neither includes direct URLs, tokens or environment values. No requirements/pyproject files were present at the deployed backend root; a fully reproducible dependency lock is not claimed.

Immutable snapshot and baseline-fixture Python files are exempt from formatter changes: changing their bytes would invalidate their serving hashes. Normal candidate source is formatted and tested. The local review clone retains the upstream push guard.

Verify the snapshot without importing it:

```python
import hashlib, json
from pathlib import Path
root = Path(__file__).parent
manifest = json.loads((root / 'SERVING.SOURCE.MANIFEST.json').read_text())
for entry in manifest['files']:
    data = (root / 'backend' / entry['path']).read_bytes()
    assert len(data) == entry['bytes']
    assert hashlib.sha256(data).hexdigest() == entry['sha256']
```

The repair overlay is separately preserved under `backend/experiments/ella_queue_resilience_v4`. Review each candidate file against its matching snapshot path. Historical deployment/configuration receipts and actual handset qualification remain separate evidence.
