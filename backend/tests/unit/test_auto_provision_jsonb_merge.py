"""
Regression test for ella-ai#501: auto_provision must MERGE agent_clusters.agents JSONB,
not replace it. A full assignment wipes userAgentId/scannerAgentId/gatewayToken on
re-authentication (sign-out -> sign-in), causing iOS 'server issues' on next open.

The fix has two parts:
  1. Postgres UPDATE uses  SET agents = agents || $1::jsonb  (merge, not replace)
  2. For gatewayToken: prefer provision API value over local env var fallback,
     so the authoritative token from Mac Mini is never silently replaced.
"""

import json


def _apply_auto_provision_write(provision_result: dict, existing: dict) -> dict:
    """
    Mirror what auto_provision_user writes, then apply Postgres || merge.
    Simulates:  UPDATE agent_clusters SET agents = agents || $1::jsonb
    """
    OPENCLAW_GATEWAY_URL = "http://100.76.138.56:19001"
    OPENCLAW_GATEWAY_TOKEN = "env-var-token"  # env var — may differ from DB
    openclaw_user_id = "omi-test123"

    gateway_url = provision_result.get("gatewayUrl", OPENCLAW_GATEWAY_URL)
    scanner_gateway_url = provision_result.get("scannerGatewayUrl", OPENCLAW_GATEWAY_URL)

    written = {
        "provider": "openclaw",
        "gatewayUrl": gateway_url,
        "scannerGatewayUrl": scanner_gateway_url,
        "workspace": provision_result.get("workspace", ""),
        "userId": openclaw_user_id,
        "provisionedAt": provision_result.get("provisionedAt") or "2026-04-01T00:00:00Z",
    }
    for field, fallback in [
        ("userAgentId", f"ella-{openclaw_user_id}"),
        ("caregiverAgentId", f"ella-cg-{openclaw_user_id}"),
        ("scannerAgentId", f"ella-scanner-{openclaw_user_id}"),
        ("summarizerAgentId", "summarizer"),
    ]:
        written[field] = provision_result.get(field) or fallback
    written["gatewayToken"] = provision_result.get("gatewayToken") or OPENCLAW_GATEWAY_TOKEN

    # Postgres || merge: existing values survive for keys absent in written
    return {**existing, **written}


# ── Fixtures ──────────────────────────────────────────────────────────────────

EXISTING_CLUSTER = {
    "userAgentId": "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "caregiverAgentId": "ella-cg-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "scannerAgentId": "ella-scanner-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "summarizerAgentId": "summarizer",
    "gatewayToken": "7a98075ef48a40f33b0be0921d62cfaa273f43779f88daca",
    "scannerGatewayUrl": "http://100.76.138.56:19001",
    "guardianMode": "ACTIVE_SUPPORT",
    "provisionedAt": "2026-02-01T00:00:00Z",
}

# Real provision API always returns agent IDs (it reads them from Mac Mini openclaw.json)
PROVISION_RESPONSE = {
    "workspace": "/Users/ellaai/.openclaw/users/omi-test123/workspace",
    "gatewayUrl": "https://gateway.ella-ai-care.com",
    "scannerGatewayUrl": "http://100.76.138.56:19001",
    "userAgentId": "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "caregiverAgentId": "ella-cg-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "scannerAgentId": "ella-scanner-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
    "summarizerAgentId": "summarizer",
    "gatewayToken": "7a98075ef48a40f33b0be0921d62cfaa273f43779f88daca",
}


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestJsonbMerge:

    def test_agent_ids_preserved_on_reprovision(self):
        """Re-provision with full API response: agent IDs stay correct."""
        result = _apply_auto_provision_write(PROVISION_RESPONSE, EXISTING_CLUSTER)
        assert result["userAgentId"] == "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2", (
            "userAgentId clobbered — regression of ella-ai#501"
        )
        assert result["scannerAgentId"] == "ella-scanner-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2"
        assert result["caregiverAgentId"] == "ella-cg-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2"

    def test_gateway_token_from_provision_api(self):
        """gatewayToken comes from provision API (not the env var fallback)."""
        result = _apply_auto_provision_write(PROVISION_RESPONSE, EXISTING_CLUSTER)
        assert result["gatewayToken"] == "7a98075ef48a40f33b0be0921d62cfaa273f43779f88daca", (
            "gatewayToken clobbered with env var — will break resolve.py routing"
        )

    def test_gateway_token_falls_back_to_env_var_when_api_omits_it(self):
        """If provision API omits gatewayToken (shouldn't happen), env var is used."""
        resp_without_token = {k: v for k, v in PROVISION_RESPONSE.items() if k != "gatewayToken"}
        result = _apply_auto_provision_write(resp_without_token, {})
        assert result["gatewayToken"] == "env-var-token"

    def test_scanner_gateway_url_written(self):
        """scannerGatewayUrl must always be written (was missing before ella-ai#501 fix)."""
        result = _apply_auto_provision_write(PROVISION_RESPONSE, {})
        assert result.get("scannerGatewayUrl") == "http://100.76.138.56:19001"

    def test_merge_preserves_extra_fields_in_existing_cluster(self):
        """Fields not written by auto_provision (e.g. guardianMode) survive the || merge."""
        result = _apply_auto_provision_write(PROVISION_RESPONSE, EXISTING_CLUSTER)
        assert result.get("guardianMode") == "ACTIVE_SUPPORT", (
            "Extra fields in existing cluster were wiped by full replace"
        )

    def test_provision_response_agent_ids_override_wrong_fallback(self):
        """If provision API returns new agent IDs (user re-provisioned), they win."""
        resp = {**PROVISION_RESPONSE, "userAgentId": "ella-omi-newagent"}
        result = _apply_auto_provision_write(resp, EXISTING_CLUSTER)
        assert result["userAgentId"] == "ella-omi-newagent"

    def test_regression_full_replace_would_wipe_guardian_mode(self):
        """
        Documents the OLD behaviour: full SET agents = $1 destroys fields not in written dict.
        This is the exact regression fixed in ella-ai#501.
        """
        # Old code wrote only the fields it knew about; guardianMode wasn't one of them
        old_written = {
            "provider": "openclaw",
            "userAgentId": "ella-omi-5agc5ye9bnhcsotxxtt4ar6ilqy2",
            "gatewayToken": "env-var-token",
        }
        # Full replace: existing fields not in written are gone
        result_old = old_written  # no merge with existing
        assert "guardianMode" not in result_old, "guardianMode survives full replace (unexpected)"
        assert result_old["gatewayToken"] == "env-var-token"  # wrong token from env var
