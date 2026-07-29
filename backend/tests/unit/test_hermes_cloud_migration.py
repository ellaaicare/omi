from pathlib import Path


def test_hermes_cloud_migration_contains_fail_closed_pool_invariants():
    migration = (
        (Path(__file__).resolve().parents[2] / "migrations" / "009_create_hermes_cloud_runtime_pool.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    required = (
        "alter column user_id drop not null",
        "ella_runtime_bindings_cloud_pool_shape_check",
        "where provider <> 'hermes_cloud'",
        "status = 'pool_available'",
        "status = 'claiming'",
        "status in ('shadow', 'internal_canary', 'active')",
        "status in ('claiming', 'shadow', 'internal_canary', 'active')",
        "ella_runtime_bindings_pool_lookup_idx",
        "for update skip locked",
        "ella_runtime_session_scopes_binding_role_channel_key",
        "request_hash varchar(64) not null",
        "prompt_artifact_receipt jsonb not null",
        "external_side_effects jsonb not null",
        "rollback_receipt jsonb not null",
        "manual_intervention_at timestamptz",
        "on delete set null",
        "active = (status <> 'shadow')",
        "ella_runtime_interactions_scope_client_key",
        "ella_runtime_ingestion_event_revision_key",
        "ella_runtime_pool_alerts_one_pending_key",
        "scope_type in ('global', 'user', 'provider', 'channel')",
        "ella_photon_channel_bindings_one_owner_key",
        "allow_all = false",
        "daily_message_limit >= 2 and daily_message_limit < 5000",
        "daily_initiation_limit > 0 and daily_initiation_limit < 50",
        "ella_photon_message_receipts_inbound_key",
        "ella_photon_message_receipts_outbound_key",
        "ella_photon_message_receipts_delivery_key",
        "consent_grant_epoch varchar(96) not null",
        "attempt_count integer not null default 1",
        "lease_token uuid",
        "lease_expires_at timestamptz",
        "'manual_required'",
        "ella_photon_quota_buckets",
    )
    # SKIP LOCKED is repository behavior rather than DDL; verify it separately.
    for fragment in required:
        if fragment == "for update skip locked":
            repository = (
                (Path(__file__).resolve().parents[2] / "database" / "ella_provisioning.py")
                .read_text(encoding="utf-8")
                .lower()
            )
            assert fragment in repository
        else:
            assert fragment in migration


def test_cloud_profile_classification_defaults_real_and_is_constrained():
    migration = (
        (Path(__file__).resolve().parents[2] / "migrations" / "010_add_cloud_profile_class.sql")
        .read_text(encoding="utf-8")
        .lower()
    )

    assert "profile_class text not null default 'real'" in migration
    assert "check (profile_class in ('real', 'synthetic'))" in migration
    assert "users_profile_class_idx" in migration


def test_runtime_authority_migrations_and_documented_psql_are_fail_fast_atomic():
    backend = Path(__file__).resolve().parents[2]
    for filename in (
        "011_create_invitation_redemption.sql",
        "012_create_account_profile_runtime_targets.sql",
        "013_create_managed_cloud_consent_authority.sql",
    ):
        migration = (backend / "migrations" / filename).read_text(encoding="utf-8").strip()
        statements = [
            line.strip() for line in migration.splitlines() if line.strip() and not line.lstrip().startswith("--")
        ]
        assert statements[0] == "BEGIN;"
        assert migration.endswith("COMMIT;")

    runbook = (backend / "ella" / "docs" / "HERMES_CLOUD_RUNTIME_TARGETS_RUNBOOK.md").read_text(encoding="utf-8")
    documented_commands = [
        line.strip()
        for line in runbook.splitlines()
        if line.strip().startswith("psql ")
        and "--file=" in line
        and (
            "011_create_invitation_redemption.sql" in line
            or "012_create_account_profile_runtime_targets.sql" in line
            or "013_create_managed_cloud_consent_authority.sql" in line
        )
    ]
    assert len(documented_commands) == 3
    assert all("-X" in command for command in documented_commands)
    assert all("--set=ON_ERROR_STOP=1" in command for command in documented_commands)
