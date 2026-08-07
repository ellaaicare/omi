import ast
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
PROTECTED_TABLES = {
    "ella_managed_cloud_consent_authority",
    "voice_entitlements",
    "ella_runtime_targets",
    "ella_runtime_bindings",
    "users",
}
MUTATION_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+"
    r"(ella_managed_cloud_consent_authority|voice_entitlements|"
    r"ella_runtime_targets|ella_runtime_bindings|users)\b",
    re.IGNORECASE,
)
USER_MUTATION_RE = re.compile(
    r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+users\b",
    re.IGNORECASE,
)

EXPECTED_WRITERS = {
    ("database/authority_advisory_lock.py", "verify_self_owner_after_lock_or_bootstrap"),
    ("database/ella_provisioning.py", "activate_runtime_binding"),
    ("database/ella_provisioning.py", "activate_user"),
    ("database/ella_provisioning.py", "claim_cloud_pool_binding"),
    ("database/ella_provisioning.py", "cleanup_cloud_pool_binding"),
    ("database/ella_provisioning.py", "ensure_user_identity"),
    ("database/ella_provisioning.py", "finalize_cloud_pool_claim"),
    ("database/ella_provisioning.py", "invalidate_self_hosted_authority_on_connection"),
    ("database/ella_provisioning.py", "promote_cloud_binding"),
    ("database/ella_provisioning.py", "quarantine_cloud_pool_claim"),
    ("database/ella_provisioning.py", "register_cloud_pool_binding"),
    ("database/ella_provisioning.py", "stage_runtime_binding"),
    ("database/ella_provisioning.py", "update_guardian_mode"),
    ("database/invitation_operator.py", "_cleanup_locked"),
    ("database/invitations.py", "_bind_verified_identity_on_connection"),
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
    ("database/managed_cloud_consent.py", "synchronize_denial"),
    ("database/managed_cloud_consent.py", "synchronize_grant"),
    ("database/managed_cloud_consent.py", "unlink_self_owner_account_on_deletion"),
    ("database/voice_canary.py", "delete_user_voice_data"),
    ("database/voice_canary.py", "update_entitlement_status"),
    ("database/voice_canary.py", "upsert_entitlement"),
}

DIRECT_LOCKED_WRITERS = EXPECTED_WRITERS - {
    ("database/authority_advisory_lock.py", "verify_self_owner_after_lock_or_bootstrap"),
    ("database/ella_provisioning.py", "cleanup_cloud_pool_binding"),
    ("database/ella_provisioning.py", "register_cloud_pool_binding"),
    ("database/invitation_operator.py", "_cleanup_locked"),
    ("database/ella_provisioning.py", "invalidate_self_hosted_authority_on_connection"),
    ("database/invitations.py", "_bind_verified_identity_on_connection"),
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
}

PROOF_GATED_HELPERS = {
    ("database/authority_advisory_lock.py", "verify_self_owner_after_lock_or_bootstrap"),
    ("database/ella_provisioning.py", "invalidate_self_hosted_authority_on_connection"),
    ("database/invitation_operator.py", "_cleanup_locked"),
    ("database/invitations.py", "_redeem_locked_invitation"),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"),
}
EXPECTED_GLOBAL_USER_WRITERS = {
    ("database/authority_advisory_lock.py", "verify_self_owner_after_lock_or_bootstrap"),
    ("database/ella_provisioning.py", "activate_runtime_binding"),
    ("database/ella_provisioning.py", "activate_user"),
    ("database/ella_provisioning.py", "ensure_user_identity"),
    ("database/ella_provisioning.py", "finalize_cloud_pool_claim"),
    ("database/ella_provisioning.py", "update_guardian_mode"),
    ("database/invitations.py", "_bind_verified_identity_on_connection"),
    ("database/invitation_operator.py", "_cleanup_locked"),
    ("database/managed_cloud_consent.py", "unlink_self_owner_account_on_deletion"),
    ("ella/utils/auto_provision.py", "auto_provision_user"),
}
NON_AUTHORITY_USER_WRITERS = {
    ("ella/utils/auto_provision.py", "auto_provision_user"),
}
REAL_POSTGRES_WRITER_COVERAGE = {
    ("database/authority_advisory_lock.py", "verify_self_owner_after_lock_or_bootstrap"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        "test_consent_bootstrap_creates_users_row_and_grant",
    ),
    ("database/ella_provisioning.py", "activate_runtime_binding"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"runtime_activate"',
    ),
    ("database/ella_provisioning.py", "activate_user"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"user_activate"',
    ),
    ("database/ella_provisioning.py", "claim_cloud_pool_binding"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"cloud_claim"',
    ),
    ("database/ella_provisioning.py", "ensure_user_identity"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        ('"identity_create"', '"identity_update"', '"identity_bind"'),
    ),
    ("database/ella_provisioning.py", "finalize_cloud_pool_claim"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"cloud_finalize"',
    ),
    ("database/ella_provisioning.py", "update_guardian_mode"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"guardian_mode"',
    ),
    ("database/ella_provisioning.py", "invalidate_self_hosted_authority_on_connection"): (
        "tests/postgres/test_invitation_redemption_postgres.py",
        "test_self_hosted_revoke_invalidates_authority_and_blocks_reactivation",
    ),
    ("database/ella_provisioning.py", "promote_cloud_binding"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"cloud_promote"',
    ),
    ("database/ella_provisioning.py", "quarantine_cloud_pool_claim"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"cloud_quarantine"',
    ),
    ("database/ella_provisioning.py", "stage_runtime_binding"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"runtime_stage"',
    ),
    ("database/invitations.py", "_redeem_locked_invitation"): (
        "tests/postgres/test_invitation_redemption_postgres.py",
        "test_broker_lock_blocks_invitation_before_capacity_or_entitlement_mutation",
    ),
    ("database/invitations.py", "_bind_verified_identity_on_connection"): (
        "tests/postgres/test_invitation_redemption_postgres.py",
        "test_self_hosted_redemption_binds_verified_email_identity_and_target_atomically",
    ),
    ("database/invitation_operator.py", "_cleanup_locked"): (
        "tests/postgres/test_invitation_redemption_postgres.py",
        "test_operator_revoke_cleanup_is_exact_and_real_user_safe",
    ),
    ("database/managed_cloud_consent.py", "_quarantine_on_connection"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"consent_regrant"',
    ),
    ("database/managed_cloud_consent.py", "lock_or_bootstrap_grant_on_connection"): (
        "tests/postgres/test_invitation_redemption_postgres.py",
        "test_broker_lock_blocks_invitation_before_capacity_or_entitlement_mutation",
    ),
    ("database/managed_cloud_consent.py", "synchronize_denial"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        "test_broker_lock_blocks_omi_revoke_until_release_without_partial_mutation",
    ),
    ("database/managed_cloud_consent.py", "synchronize_grant"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"consent_grant"',
    ),
    ("database/managed_cloud_consent.py", "unlink_self_owner_account_on_deletion"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        "test_delete_unlinks_users_row_and_consent_authority_freeing_uid",
    ),
    ("database/voice_canary.py", "delete_user_voice_data"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"entitlement_delete"',
    ),
    ("database/voice_canary.py", "update_entitlement_status"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"entitlement_status"',
    ),
    ("database/voice_canary.py", "upsert_entitlement"): (
        "tests/postgres/test_authority_advisory_lock_postgres.py",
        '"entitlement_upsert"',
    ),
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


def _functions_with_global_user_mutations():
    writers = {}
    for directory in ("database", "ella", "scripts"):
        for path in sorted((BACKEND / directory).rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                    continue
                mutation_strings = [
                    item.value
                    for item in ast.walk(node)
                    if isinstance(item, ast.Constant)
                    and isinstance(item.value, str)
                    and USER_MUTATION_RE.search(item.value)
                ]
                if mutation_strings:
                    key = (str(path.relative_to(BACKEND)), node.name)
                    writers[key] = {
                        "source": ast.get_source_segment(source, node) or "",
                        "mutations": mutation_strings,
                    }
    return writers


def test_protected_authority_writer_inventory_is_closed_and_explicit():
    writers = _functions_with_protected_mutations()
    assert set(writers) == EXPECTED_WRITERS
    assert set().union(*(entry["tables"] for entry in writers.values())) == PROTECTED_TABLES
    assert not any(path.startswith("scripts/") for path, _function in writers)


def test_global_users_writer_inventory_is_closed_and_authority_scoped():
    writers = _functions_with_global_user_mutations()
    assert set(writers) == EXPECTED_GLOBAL_USER_WRITERS
    assert set(writers) - NON_AUTHORITY_USER_WRITERS <= EXPECTED_WRITERS

    legacy_phone_write = writers[("ella/utils/auto_provision.py", "auto_provision_user")]
    assert len(legacy_phone_write["mutations"]) == 1
    statement = legacy_phone_write["mutations"][0]
    set_clause = statement.upper().split(" SET ", 1)[1].split(" WHERE ", 1)[0]
    assert "IDENTITIES =" in set_clause
    assert not any(
        f"{column.upper()} =" in set_clause for column in ("id", "omi_uid", "email", "profile_class", "status")
    )


def test_every_owner_bound_writer_is_pinned_to_real_postgres_contention_coverage():
    assert set(REAL_POSTGRES_WRITER_COVERAGE) == EXPECTED_WRITERS - {
        ("database/ella_provisioning.py", "cleanup_cloud_pool_binding"),
        ("database/ella_provisioning.py", "register_cloud_pool_binding"),
    }
    for key, (relative_path, marker_spec) in REAL_POSTGRES_WRITER_COVERAGE.items():
        source = (BACKEND / relative_path).read_text(encoding="utf-8")
        markers = (marker_spec,) if isinstance(marker_spec, str) else marker_spec
        assert all(marker in source for marker in markers), key


def test_every_owner_bound_writer_has_direct_lock_or_lock_proof():
    writers = _functions_with_protected_mutations()
    for key in DIRECT_LOCKED_WRITERS:
        source = writers[key]["source"]
        assert "acquire_authority_lock(" in source, key
        verification_indices = [
            source.index(token)
            for token in (
                "verify_self_owner_after_lock_or_bootstrap(",
                "verify_self_owner_after_lock(",
                "verify_identity_owner_after_lock(",
            )
            if token in source
        ]
        assert verification_indices, key
        assert re.search(
            r"async with [^\n]+\.transaction\(\):\n"
            r"\s+owner_lock = await authority_advisory_lock"
            r"\.acquire_authority_lock\(",
            source,
        ), key
        assert "proof=owner_lock" in source, key
        first_mutation = min(
            index for token in ("INSERT INTO", "UPDATE ", "DELETE FROM") if (index := source.find(token)) >= 0
        )
        verification_index = min(verification_indices)
        assert source.index("acquire_authority_lock(") < verification_index, key
        assert verification_index < first_mutation, key
    for key in PROOF_GATED_HELPERS:
        source = writers[key]["source"]
        assert "owner_lock" in source, key
        if key[1] != "_redeem_locked_invitation":
            assert "require_self_owner_lock(" in source or "verify_identity_owner_after_lock(" in source, key


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
