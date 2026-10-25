# Closed transaction diagnostics candidate

Source-only observability correction; not deployed. Current-serving baseline SHA256 is `7dd2607728a5f26cfd8305ff558e7adbf26ddad4cf6e0bd9bb6b71bb96228784`. The candidate adds fixed exception labels, fixed source filenames, and four known writeback codes. All function bodies and imports are unchanged. Messages, locals, arbitrary arguments, private paths and unknown fields remain excluded.

Eight owner tests and five independent privacy/bounds cases passed. Packaged tests use actual installed Google API exception classes and extract the error class from the hash-pinned adjacent queue candidate. This is credential-free offline composition, not Firestore or service import acceptance. Only the archive dependency path differs. Run `python3 -m unittest discover -s backend/experiments/ella_transaction_causal_diagnostics_v1/tests -v` from the repository.

A synthetic Unknown→RetryError→DeadlineExceeded chain is masked as three OtherException labels by baseline and survives as class names in candidate. This does **not** establish the historical failed attempt's actual inner exception, which was not retained. No live inference, source, quarantine, policy, credential or data mutation is included. The separate transaction-callback correction needs direct application tests before another controlled saved-memory attempt.
