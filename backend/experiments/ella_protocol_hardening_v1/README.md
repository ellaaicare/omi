# Defensive OpenRouter response hardening

This reviewed candidate prevents two false-success cases reproduced against the installed transport with fake HTTP: a non-null per-choice error ignored beside an otherwise valid stop response, and a streaming-object response accepted as non-streaming. The change rejects non-null root/choice errors and requires `object="chat.completion"`. These are defensive corrections, not an explanation or recovery fix for the historical nonterminal rejection.

Existing complete-response, single-choice/index, model, role, no-tool/refusal, JSON, size, owner/runtime, one-use and deadline guards remain intact. Missing/null errors can proceed; malformed falsey errors cannot. Diagnostic additions contain only closed envelope/error facts and preserve older metadata compatibility. No prompt, response content, reasoning text or credential is retained.

Independent review ran 21 offline transport/projector tests. The suite includes serving-baseline false-success reproduction, valid terminal cases, error/length/tool/refusal/partial failures, missing/wrong object and index negatives, and old/new metadata validation. Fake responses do not qualify a real model, memory or writeback. No inference, saved-record admission, deployment or route change is part of this candidate.

The [overview](https://openrouter.ai/docs/api_reference/overview) documents optional per-choice errors. The [endpoint schema](https://openrouter.ai/docs/api/api-reference/chat/create-a-chat-completion) requires the non-streaming completion object. Incomplete overview examples are not evidence that choice-index validation should be relaxed; it remains unchanged.

Deployment is not necessary for the blocked historical metadata investigation. Keep this candidate unmerged and undeployed until its validation benefit is included in a focused release decision; do not use it to justify consuming another saved source.
