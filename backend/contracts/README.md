# Cross-service authority lock

`authority_advisory_lock_v1.json` is vendored byte-for-byte from
`ellaaicare/ella-ai#1155` at
`ed216b71f6350a46fda847843fee4fedc0e5cb9f`. Its reviewed SHA-256 is:

```text
1a92c0d335742560fff71b4630b95e1424bccdafb15c6245c8a554f4ea19eb69
```

`database/authority_advisory_lock.py` verifies that digest at runtime and
implements the contract's six vectors. Owner-bound OMI authority writers resolve
the candidate account/profile outside the mutation transaction, take the v1
transaction lock as the first statement, then lock and verify ownership before
any protected row lock or mutation.

Lock acquisition returns an opaque, non-copyable proof registered only by the
contract module. Every proof-gated mutation rechecks that the same asyncpg
connection is in the same PostgreSQL transaction and still owns the exact
advisory lock. Proofs fail closed when forged, moved to another connection, or
reused after commit or rollback.

The cross-repo derivation helpers accept canonical UUID text only. OMI validates
that asyncpg ownership values are `uuid.UUID` instances at the database boundary,
then explicitly converts those trusted values to text before deriving the key.
Implicit stringification is rejected.

Identity creation uses a fixed UUIDv5 namespace plus the verified Firebase UID
to derive a deterministic provisional self-owner. The writer locks that key
before any user row lock or insert, then re-reads both UID and email ownership.
An existing row with any other owner fails closed. Existing identity updates,
activation, and runtime-driven user activation use the resolved owner key and
perform the same locked re-read before mutation.

The sole authority-neutral writer is warm-pool registration. It may only insert
an unowned, inactive `pool_available` row and may never update or claim an
owner-bound row. The writer-inventory guard fails if that SQL shape changes or a
new protected-table writer appears. The legacy Mini auto-provision path's
`users.identities` phone merge is separately inventoried as non-authoritative:
it cannot alter user ID, UID, email, profile class, or status, and widening that
statement fails the guard.
