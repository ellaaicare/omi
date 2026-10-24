# Thin exact-source MCP adapter correction

The adapter lacked the shared build_summary_version_update API. Its whole-document set also persisted dotted structured.title/overview updates as literal keys, leaving nested presentation stale after an otherwise successful report/version/ledger commit.

This minimal correction statically delegates to the existing shared version builder and uses the existing protection transform followed by native transaction.update for the builder update map. Owner/receipt/epoch/source/pending/CAS guards remain. Original compressed/encrypted transcript and app-result bytes are not rewritten by the summary update.

Owner10pure cases and independent actual shared report/CAS/current composition plus installed Firestore SDK field-path extraction passed. Full module import in the actual service interpreter passed with network blocked. Authority/storage/enhanced-crypto boundaries were synthetic in transaction tests; real capture/MCP persistence is separate. No physical capture or fictional source is implied, and there is no new ingress/grant. Source-only package; deployment/health and real source acceptance remain independently recorded.
