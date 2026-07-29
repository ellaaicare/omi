import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROTECTED_TABLES = {
    "ella_managed_cloud_consent_authority",
    "voice_entitlements",
    "ella_runtime_targets",
    "ella_runtime_bindings",
}
MUTATION_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(ella_managed_cloud_consent_authority|voice_entitlements|"
    r"ella_runtime_targets|ella_runtime_bindings)\b",
    re.IGNORECASE,
)

EXPECTED_WRITERS = {
    ("database/ella_provisioning.py", "activate_runtime_binding"),
    ("database/ella_provisioning.py", "claim_cloud_pool_binding"),
    ("database/ella_provisioning.py", "finalize_cloud_pool_claim"),
    ("database/ella_provisioning.py", "promote_cloud_binding"),
    ("database/ella_provisioning.py", "quarantine_cloud_pool_claim"),
    ("database/ella_provisioning.py", "register_cloud_pool_binding"),
    ("database/ella_provisioning.py", "stage_runtime_binding"),
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
    ("database/managed_cloud_consent.py", "synchronize_denial"),
    ("database/managed_cloud_consent.py", "synchronize_grant"),
    ("database/voice_canary.py", "delete_user_voice_data"),
    ("database/voice_canary.py", "update_entitlement_status"),
    ("database/voice_canary.py", "upsert_entitlement"),
}

DIRECT_LOCKED_WRITERS = EXPECTED_WRITERS - {
    ("database/ella_provisioning.py", "register_cloud_pool_binding"),
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
}

PROOF_GATED_HELPERS = {
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
}


def _functions_with_protected_mutations():
    writers = {}
    for directory in ("database", "scripts"):
        for path in sorted((BACKEND / directory).glob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                strings = "\n".join(
                    item.value
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant) and isinstance(item.value, str)
                )
                tables = set(MUTATION_RE.findall(strings))
                if tables:
                    key = (str(path.relative_to(BACKEND)), node.name)
                    writers[key] = {
                        "source": ast.get_source_segment(source, node) or "",
                        "tables": {table.lower() for table in tables},
                    }
    return writers


def test_protected_authority_writer_inventory_is_closed_and_explicit():
    writers = _functions_with_protected_mutations()
    assert set(writers) == EXPECTED_WRITERS
    assert set().union(*(entry["tables"] for entry in writers.values())) == PROTECTED_TABLES
    assert not any(path.startswith("scripts/") for path, _function in writers)


def test_every_owner_bound_writer_has_direct_lock_or_lock_proof():
    writers = _functions_with_protected_mutations()
    for key in DIRECT_LOCKED_WRITERS:
        source = writers[key]["source"]
        assert "acquire_authority_lock(" in source, key
        assert re.search(
            r"async with [^\n]+\.transaction\(\):\n"
            r"\s+(?:owner_lock = )?await authority_advisory_lock"
            r"\.acquire_authority_lock\(",
            source,
        ), key
        assert source.index("acquire_authority_lock(") < min(
            index for token in ("INSERT INTO", "UPDATE ", "DELETE FROM") if (index := source.find(token)) >= 0
        ), key
    for key in PROOF_GATED_HELPERS:
        source = writers[key]["source"]
        assert "owner_lock" in source, key
        if key[1] != "_redeem_locked_invitation":
            assert "require_self_owner_lock(" in source, key


def test_unowned_pool_registration_is_the_only_authority_neutral_exception():
    source = _functions_with_protected_mutations()[("database/ella_provisioning.py", "register_cloud_pool_binding")][
        "source"
    ]
    assert "'pool_available'" in source
    assert "$1, NULL, 'user', 'hermes_cloud', 'pool_available'" in source
    assert "'healthy', $16::jsonb, 1, false" in source
    assert "DO NOTHING" in source
    assert "DO UPDATE" not in source
    assert "runtime_instance_already_claimed" in source
