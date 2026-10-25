# Exact-source LF evidence projection

Source-qualified; **not deployed**. No new inference or record admission was used to diagnose or test this correction.

The retained failure had two evidence quotes with one LF where the canonical transcript contained a blank line. Both corresponded to unique original source spans. All non-LF characters, case, punctuation and speaker labels matched. Three other quotes were already exact.

The new helper resolves only literal-LF run multiplicity in transcript evidence, on a copy. It returns original source bytes, with codepoint interval, segment text digest and raw/resolved quote digests. Exact quotes keep prior behavior; ambiguous fallback matches, invented words, removed speaker labels, case, spaces, tabs, CR and Unicode changes remain rejected. Contextual-addition evidence is unchanged. This is location/format proof, never semantic entailment or authority.

The original generator result remains unchanged in its ledger and canonical event. The verifier receives the derived copy and versioned proof. Stored review reconstructs the same request from original retained output and frozen source; altered proofs/requests fail. Shared strict validation, source/owner/version fences, CAS and publication checks remain intact. The resolver contract is bound into generator/verifier request identity and fresh admission policy revision. Old retained requests are not reinterpreted or replayed.

Nineteen actual pure contract/producer cases and six independent boundary/proof cases passed. Model/storage leaves in orchestration tests are explicit doubles, not real integration. Actual service Python 3.11 imports passed with networking blocked. A separate backend-only read test resolved exactly the two retained mismatches and passed the unchanged strict transcript validator without changing the raw result or source. This does not establish contextual additions, a real verifier verdict, writeback or recall.

Run the bounded portable standard-library fixture with `python3 run_offline_tests.py`. It hash-checks the published source copies, materializes a disposable small tree, rebases one test-only baseline path, and runs the same 19+6 cases. No SDK, credential, database or model dependency is used. The `.py.source` files are review artifacts; merely copying this directory does not deploy them.

Before another paid test: independently reviewed rollout, actual service/source checks, credible remaining context-read latency budget, and a parent-coordinated Mini inference window are required. The separate prelude optimization is not included here. No held output is authorized for publication by these tests.
