from pathlib import Path


def test_hermes_cloud_migration_contains_fail_closed_pool_invariants():
    migration = (
        Path(__file__).resolve().parents[2]
        / "migrations"
        / "009_create_hermes_cloud_runtime_pool.sql"
    ).read_text(encoding="utf-8").lower()

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
    )
    # SKIP LOCKED is repository behavior rather than DDL; verify it separately.
    for fragment in required:
        if fragment == "for update skip locked":
            repository = (
                Path(__file__).resolve().parents[2]
                / "database"
                / "ella_provisioning.py"
            ).read_text(encoding="utf-8").lower()
            assert fragment in repository
        else:
            assert fragment in migration
