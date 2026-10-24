# Bounded caller budget increase

Dedicated retained semantic caller max_tokens1600 becomes4096. The original rationale for1600 is not recorded; it is not a per-key setting or universal Hermes chat limit. Current OpenRouter transport requests reasoning effort none; existing8192 ceiling,90-second provider/600-second operation deadlines stay. Producer16,000-byte result limit stays independently enforced.

Three tests extract the actual caller literal and pass it through actual transport fakeHTTP, preserve length rejection and compare ASTs for the single constant delta. Not a full async caller integration test. Tests use sibling ../ella_reasoning_budget_v1/tests.

Not a fix for separate response_invalid JSON-object rejection or permission to accept incomplete/tool outputs. Does not configure Codex: installed OAuth-Codex wire omits explicit output cap and requires separately qualified contract work. Source review, deployment and acceptance are separate. No credentials/transcript data included.
