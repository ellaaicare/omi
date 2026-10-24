# Closed semantic completion metadata

Diagnostic-only host overlay. The retained failed attempt received HTTP 200 and full EOF, then hit `response_nonterminal_or_tool`; its finish reason and usage were not retained. Token exhaustion is not established. No uncertain request is replayed by this patch.

The transport projects only closed finish/role/shape/tool/refusal facts, bounded token counts and a restricted opaque generation ID before the existing rejection. The worker validates that projection. No prompt, content, reasoning text, URL or credential is retained. Existing completion guards, model, provider, requested token limit and deadlines are unchanged.

Independent source review: 13 offline tests passed. Actual gateway Python 3.11.14 imported the staged five-module source closure with network and credential discovery blocked: zero attempts. These import and fake-provider tests are not real inference or serving-success proof. Deployment review and source-origin verification are separate.

Operational targets are the existing host gateway/platforms `semantic_openrouter_transport.py` and `semantic_role_worker.py`. Baseline files are exact source bytes with `.source` suffix to preserve historical hashes. Source review and staged imports do not recover missing historical response metadata.
