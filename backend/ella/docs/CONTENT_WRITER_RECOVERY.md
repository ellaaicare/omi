# Durable content-writer recovery

Account deletion remains `DRAINING` while any durable content writer is
registered. Lease expiry is diagnostic only and never authorizes release. Use
`scripts/content_writer_recovery.py` only for a writer orphaned by an actual
process exit.

## Trust boundary and deployment requirement

The backend Docker image starts one Uvicorn process. Each writer registration
is bound at process startup/first use to an internally generated immutable
generation plus the Linux-kernel-observed hashed boot-bound host identity,
boot ID, PID namespace, PID, and process start identity. Request data cannot
choose these fields. Production recovery is Linux-only. Darwin and every other
system refuse before application, Google, configuration, or credential imports;
the Darwin process adapter under `tests/support/` is test-only and the production
CLI has no flag or environment escape hatch that can select it.

The recovery trusted computing base is deliberately narrow:

- effective UID 0, effective `CAP_SYS_ADMIN`, and a successful kernel
  `NS_GET_PARENT` proof that the command is in the initial host PID namespace
  are jointly required before any authority-bearing import or file read;
- the kernel process table in the recorded host, boot, and PID namespace is
  authoritative for the process and all of its threads;
- the durable Firestore transaction is authoritative for the final exact
  subject-document, token, and owner-generation compare-and-set.

Process exit is terminal for every thread in that process. The command accepts
no terminal boolean, lease claim, owner JSON, PID, generation, host override,
or remote proof. It is not registered as an HTTP route.

The command must run in the same initial-host supervisor boundary that observed
the worker. Normal container root, nested namespace root, a privileged nested
PID namespace, and a sidecar are not command authorities. The production
bootstrap refuses all of them before imports, config reads, credential reads,
or output. After authorization it scans the host process table for the exact
recorded PID namespace, namespace-local PID, and process start identity.
Another worker, another host, another boot, an unknown namespace, a hidden
process table, or an unavailable supervisor capability cannot prove absence
and therefore cannot recover the record. Those cases intentionally remain
`DRAINING`. Do not copy owner fields to make a replacement look local.

## Fixed release and Firestore authority

The recovery executable is installed separately from the application container
at `/opt/omi-recovery/backend/scripts/content_writer_recovery.py` and must run
with isolated Python (`-I`). The script, `/opt/omi-recovery/backend`, its Python
executable, and every parent path must be root-owned and not group/world
writable. The script itself may not be a symlink. Cwd, `PYTHONPATH`, user site,
and interpreter environment settings are excluded before the release root is
added to `sys.path`.

The only configuration path is
`/etc/omi/content-writer-recovery.json`. There is no CLI or environment option
for another path. It has this non-secret shape:

```json
{
  "schema_version": 1,
  "project_id": "pinned-production-project",
  "database_id": "(default)",
  "credential_file": "/etc/omi/content-writer-recovery-service-account.json",
  "deployment_receipt_file": "/etc/omi/content-writer-recovery-receipt.json"
}
```

The config, credential, and deployment-receipt files must be regular,
root-owned, mode `0600` (or more restrictive), reached through a root-owned
non-writable parent chain, and opened with `O_NOFOLLOW`. The receipt pins the
same project/database and the SHA-256 of the credential file. The service
account's own project must match. Firestore receives explicit project,
database, and credential objects; ambient `GCLOUD_PROJECT`,
`GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`, and
`SERVICE_ACCOUNT_JSON` cannot select authority. `FIRESTORE_EMULATOR_HOST` is a
hard refusal. This path never imports `database._client` and never materializes
credential JSON in cwd or any derived path.

## Deterministic recovery sequence

1. Stop admission for the affected deployment generation. Keep Firebase
   authentication and all user content retained.
2. From the recorded host/boot/PID namespace, verify the exact worker has
   reached the supervisor's terminal state. Do not restart or reuse its
   namespace before recovery.
3. In the initial-host supervisor shell, derive SHA-256 selectors for the exact
   UID and writer token without writing plaintext to logs or receipts.
4. Run the fixed root-owned release (not the application checkout or container):

   ```text
   sudo /opt/omi-recovery/venv/bin/python3 -I \
     /opt/omi-recovery/backend/scripts/content_writer_recovery.py \
     --subject-hash <64-lowercase-hex> \
     --token-hash <64-lowercase-hex>
   ```

5. Accept only exit code `0` with `result` equal to `recovered` or
   `already_recovered`. The JSON receipt is content-free: it contains only
   hashes, an owner fingerprint, the kernel proof kind, and the result.
6. Retry the ordinary authenticated `DELETE /v1/users/delete-account` flow.
   That retry advances the empty fence transactionally, performs exact purges,
   and deletes Firebase authentication last.

## Abort, retry, and rollback behavior

Any nonzero exit is an abort. Leave the durable record unchanged, leave
Firebase retained, and investigate the content-free reason. In particular,
never convert `owner_live`, `pid_reused`, host/boot/namespace mismatch,
owner-unknown, stale-token, record-replaced, corrupt, or unavailable results
into a manual release.

The recovery compare-and-set removes only the exact subject document's exact
token when its complete owner identity still matches the terminal proof. It
stores one content-free latest receipt (`token_hash`, exact owner, timestamp),
not recovery history. Its serialized size is therefore constant regardless of
crash count. Any reviewed pre-release map shape is compacted to its latest
validated receipt on the next transaction. Concurrent retries of that exact
transaction converge; an absent,
evicted, stale, colliding, or different-token receipt authorizes nothing.
Ordinary ACTIVE cleanup deletes an empty fence even if a receipt exists, and
tombstoning removes the receipt, so it cannot retain a fence document or block
deletion. Other tokens and other subjects remain untouched. A successful
release has no safe manual rollback because recreating an operator-authored
owner record would forge ownership. If the subsequent account-deletion retry
fails, keep Firebase retained and retry the ordinary deletion flow; do not
recreate the writer.
