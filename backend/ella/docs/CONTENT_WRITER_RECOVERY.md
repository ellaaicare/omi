# Durable content-writer recovery

Account deletion remains `DRAINING` while any durable content writer is
registered. Lease expiry is diagnostic only and never authorizes release. Use
`scripts/content_writer_recovery.py` only for a writer orphaned by an actual
process exit.

## Trust boundary and deployment requirement

The backend Docker image starts one Uvicorn process. Each writer registration
is bound at process startup/first use to an internally generated immutable
generation plus the kernel-observed system, hashed boot-bound host identity,
boot ID, PID namespace, PID, and process start identity. Request data cannot choose
these fields.

The recovery trusted computing base is deliberately narrow:

- local root is the only command authority;
- the kernel process table in the recorded host, boot, and PID namespace is
  authoritative for the process and all of its threads;
- the durable Firestore transaction is authoritative for the final exact
  subject-document, token, and owner-generation compare-and-set.

Process exit is terminal for every thread in that process. The command accepts
no terminal boolean, lease claim, owner JSON, PID, generation, host override,
or remote proof. It is not registered as an HTTP route.

The command must run in the same supervisor boundary that observed the worker.
For Docker's single-Uvicorn image, run it on the same-boot host as root with the
host `/proc` mounted: cross-namespace proof additionally requires effective
`CAP_SYS_ADMIN` and a kernel `NS_GET_PARENT` check proving the command itself is
in the initial host PID namespace. It then scans the host process table for the
exact recorded PID namespace, namespace-local PID, and process start identity.
Container-root, including a privileged nested PID namespace, cannot infer that
another container is dead. A sidecar in a shared pod PID namespace can use the
same-namespace path.
Another worker, another host, another boot, an unknown namespace, a hidden
process table, or an unavailable supervisor capability cannot prove absence
and therefore cannot recover the record. Those cases intentionally remain
`DRAINING`. Do not copy owner fields to make a replacement look local.

## Deterministic recovery sequence

1. Stop admission for the affected deployment generation. Keep Firebase
   authentication and all user content retained.
2. From the recorded host/boot/PID namespace, verify the exact worker has
   reached the supervisor's terminal state. Do not restart or reuse its
   namespace before recovery.
3. In a root-only shell, derive SHA-256 selectors for the exact UID and writer
   token without writing plaintext to logs or receipts.
4. Run from `backend/`:

   ```text
   sudo .venv/bin/python scripts/content_writer_recovery.py \
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
token when its complete owner identity still matches the terminal proof.
Concurrent and duplicate commands converge through a durable recovery receipt;
other tokens and other subjects remain untouched. A successful release has no
safe manual rollback because recreating an operator-authored owner record would
forge ownership. If the subsequent account-deletion retry fails, keep Firebase
retained and retry the ordinary deletion flow; do not recreate the writer.
