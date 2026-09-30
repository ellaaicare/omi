# Main-chat summary tools: source candidate, not a live rollout

Private issue: `ellaaicare/ella-ai#1010`.

## Source contract

The first-party Streamable HTTP MCP endpoint is `/v1/ella/plato/mcp`.
The Ella extension imports the canonical corrections router before the MCP
router (`ella/__init__.py`). Corrections explicitly registers its writer through
`ella/services/summary_tool_registry.py`; absent registration hides these tools.

- `companion_get_conversation_summary`: an explicit conversation ID, its summary,
  and exact active version. Requires `memory:read`.
- `companion_correct_conversation`: an explicit ID, user correction text,
  `expected_active_summary_version_id`, and scoped `idempotency_key`.
- `companion_resummarize_conversation`: the same exact ID/version/key, without
  correction text. Re-reads the immutable transcript and uses that owner's
  canonical Hermes OMI session. It does not assert that a factual change was requested.

Writes require `summaries:write`; no default role or static bearer gains it.
UID comes only from authenticated session claims, never tool arguments.
Signed claims and the current durable self grant must authorize the exact tool.
Grant metadata must bind `runtime_binding_id` and `profile_user_id` to the resolved
transcript runtime. The runtime must also register the tool in `allowed_tools`.
Consent, grant, rollout, and exact runtime authority are rechecked across awaited work.

Both writes use the canonical summary-only writer with atomic active-version and
transcript CAS, retained previous versions, request identity stored atomically in
the new version, durable receipt, and existing Undo route. Neither submits
propagation proposals nor uses failed-processing retry. Provider failures and
missing generated title/overview create no summary version or correction receipt.
Canonical-ledger failure after CAS leaves the existing engine's pending receipt;
replay repairs publication without regenerating or adding another version.
The receipt GET remains `pending` during that failure window, and the exact
summary read refuses unconfirmed versions. Undo cannot begin before publication
is confirmed. Undo of these new versions also requires canonical confirmation;
a failed Undo remains `pending` and an Undo replay repairs the same version
before reporting `undone`. Legacy correction/Undo behavior remains unchanged.

Signed per-user sessions expose only the three summary tools above, restricted
by their current grant and runtime registration. The global legacy Plato helpers
(including startup, search, scanner rules, and observation writes) are neither
listed nor callable through this lane, even if a token names them. Static legacy
bearers retain their existing surface and cannot access the new summary tools.

Concurrent duplicate calls can execute the provider twice. They commit one
summary version; this is not an exactly-once provider-cost guarantee. A reused
key with changed intent payload is rejected. An already-undone operation is not
reapplied by replay. Existing correction API callers remain on the old path;
supplying both optional version/key fields opts into strict synchronous CAS.

## Separate deployment and registration gate

This source does **not** attach an MCP binding to a live HermesCloud profile.
Do not describe the feature as usable from main chat until the following is
separately authorized and verified for the exact account/profile:

1. Deploy a reviewed backend head and verify both router registrations.
2. Preserve default OFF. Enable only an approved exact UID with
   `ELLA_MCP_SUMMARY_TOOLS_ENABLED=true` and `ELLA_MCP_SUMMARY_TOOLS_UIDS=<exact UID>`.
   Wildcards do not enable profiles.
3. Provision an explicit durable self-profile grant with the required scopes,
   exact tool names, and the transcript binding/profile metadata above. Issue a
   short-lived signed MCP session token using the existing MCP identity system.
   Do not use the legacy static Plato bearer for these operations.
4. Attach the first-party endpoint and profile-scoped credentials through the
   actual HermesCloud `mcp_binding`/tool registry. That registry is not configured
   by this repository; this step needs its owner. Do not place credentials in docs.
5. Update only the approved profile's runtime tool policy and attestation.
   `HermesCloudClient.preflight()` requires observed tools to exactly equal
   `allowed_tools`; adding code alone neither publishes tools nor updates policy.
   Do not bypass a disabled account-admission/binding gate.
6. Perform read-only discovery for that signed profile: tools must remain absent
   with a missing handler, disabled rollout, missing runtime registration,
   revoked consent/grant, or mismatched runtime/profile. Confirm the exact read
   tool supplies the chosen conversation's active version.
   The binding must expect only the explicitly granted summary tools, not the
   global legacy Plato startup/read helpers. Choose the conversation explicitly
   through the existing first-party application; this lane does not list memories.
7. Only after a separate functional-canary authorization, correct/re-summarize
   one approved explicit test conversation, check receipt and raw-transcript
   invariance, then exercise Undo. No such live canary is part of this source work.

Keep this rollout separate from illustration, Dreams, capture, or iOS changes.
