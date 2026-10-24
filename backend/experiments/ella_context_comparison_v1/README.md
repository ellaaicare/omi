# Context snapshot comparison diagnostics

This source-only candidate adds closed hash/count/revision metadata before the existing `retained_completion_context_changed` rejection. It does not accept a changed context, recompute an existing result, reopen a job, alter policy, or trigger a provider. The original reservation and publication fences remain intact.

The entire retained coordinator class and actual host hydration function are exercised by seven cases. External storage, consent, resolver, canonical-event and learning leaves are explicit synthetic boundaries. Stable input reaches a deliberate producer stop after one policy commit. Changed content, missing versus empty raw files, runtime revision changes and diagnostic failure preserve rejection with zero producer calls or policy commits. This is not a real Firestore transaction or live host callback proof.

Run `python3 -B -m unittest discover -s backend/experiments/ella_context_comparison_v1/tests -v` from the repository root. The service interpreter separately imported the complete candidate with actual dependencies, checked its ABI and synthetic metadata schema, with zero database query/provider call. The serving baseline hash matched. No diagnostic rollout is implied.

The failed historical attempt retained only its rejection and an earlier sync digest/revision, not both compared snapshots or their document hashes. It cannot be reconstructed from present file hashes. A later read-only host snapshot request was rejected after quarantine; no guard was bypassed and no second snapshot was requested.

A separate real production `canonical_memory.read_memory` and rendering check retrieved the exact source transcript under the original account/profile, independently of enhanced app-summary writeback. It returned source hashes/counts only. This proves durable canonical retrieval, not an authenticated phone request, model answer, enhanced personal facts, complete backlog coverage or shared iMessage session.

Safe future recomputation must occur before model/publication effects, after proving the previous reservation did not commit, with fresh owner/consent/source/runtime validation and a newly pinned immutable context policy. It must be bounded, reject unknown commit/provider outcomes, and retain existing quarantines. No such continuation/recomputation is implemented or authorized by this diagnostics module itself.

Two additional actual-host/coordinator cases confirm that a selected file mtime-only rewrite and an unselected attempt-metadata file do not change context identity. Publication snapshots use `.py.source` to preserve the reviewed bytes through formatting hooks.
