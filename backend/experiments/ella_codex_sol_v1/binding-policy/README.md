# Retained binding model-policy alignment

A coordinated model migration must update both the managed profile policy and its retained backend binding metadata. A profile-only change left the personal binding on its old policy label, which the unchanged strict semantic authority validator correctly rejected. The existing general provisioning upsert rewrites many unrelated binding fields, so the narrow helper updates only the model-policy label, revision and timestamp of one exact existing owned binding.

The helper uses the existing typed advisory-owner lock and ownership revalidation, then an exact full-row/id/revision/policy comparison and parameterized update. It validates all immutable fields inside the transaction before commit. An already-target binding is a no-write path. It neither provisions nor changes identity, routing, credentials, consent, targets, agent/workspace, transcripts or quarantines.

Fifteen source and receipt fixtures passed. Additional actual service checks exercised the real owner/advisory-lock/proof/UUID path on an already-target lab binding without changing it, and prepared the exact SQL without execution to verify grammar and parameter types. The reviewed one-row alignment then committed, and both existing accounts passed strict semantic composition with their original routes. The policy label retains a legacy version name while the cryptographic policy digest binds the actual new model. No authority check was relaxed.

These archived fixtures retain dependency references; they are not a standalone environment. Operational cohort packets, row identifiers, transcripts, credentials and live launchers are excluded. This alignment is separate from the pending end-to-end Codex inference/writeback/recall acceptance.
