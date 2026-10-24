# Atomic one-job qualification restriction

This optional control restricts an already-authorized processing policy to one exact existing pending, never-started job. It grants no account, route, source, consent or replay authority. The normal source/account/attempt/lease/quota validators remain mandatory. With no control field, existing behavior is unchanged.

The issuance transaction compares the exact closed policy and job images, requires zero active attempts and unresolved resources, and binds job/UID/CID/client/transcript/admission epoch/account epoch/route. Claim consumes issued to reserved with its lease hash in the same transaction. Begin atomically closes new claims and marks the selected attempt started. Other jobs cannot slip through a polling window. The selected graph continues under its existing owner/source/route/lease fences while new claims are closed.

An expired, consumed, malformed or mismatched control fails closed. It is never automatically removed to replay a job. Resuming ordinary processing requires a separately guarded retirement after terminal outcome/resource reconciliation. This is a bounded qualification tool, not a replacement for normal per-job quarantine and independent healthy-job progress.

49 isolated tests pass using actual candidate functions and the sibling ella_queue_resilience_v4 synthetic serialized store. External SDK/provenance adapters are explicitly doubled; these tests do not prove live provider generation or durable enhanced writeback. Run: python3 -B tests/test_single_claim.py from this directory. The reviewed operational issuer/deployment controls remain private and are not part of this public source package. No credentials or transcripts are included.
