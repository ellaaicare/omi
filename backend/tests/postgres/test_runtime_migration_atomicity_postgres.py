import asyncio
import os
import uuid
from pathlib import Path

import asyncpg
import pytest

TEST_DSN = os.getenv("ELLA_TEST_POSTGRES_DSN", "").strip()
MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"

pytestmark = pytest.mark.skipif(
    not TEST_DSN,
    reason="ELLA_TEST_POSTGRES_DSN is required for migration atomicity tests",
)


async def _assert_failure_rolls_back(filename: str, leaked_relations: tuple[str, ...]) -> None:
    schema = f"migration_failure_{uuid.uuid4().hex}"
    admin = await asyncpg.connect(TEST_DSN)
    await admin.execute(f'CREATE SCHEMA "{schema}"')
    failing = await asyncpg.connect(
        TEST_DSN,
        server_settings={"search_path": schema},
    )
    try:
        with pytest.raises(asyncpg.PostgresError):
            await failing.execute((MIGRATIONS / filename).read_text(encoding="utf-8"))
    finally:
        await failing.close()

    verifying = await asyncpg.connect(
        TEST_DSN,
        server_settings={"search_path": schema},
    )
    try:
        for relation in leaked_relations:
            assert await verifying.fetchval("SELECT to_regclass($1)", relation) is None
    finally:
        await verifying.close()
        await admin.execute(f'DROP SCHEMA "{schema}" CASCADE')
        await admin.close()


@pytest.mark.parametrize(
    ("filename", "leaked_relations"),
    [
        (
            "011_create_invitation_redemption.sql",
            (
                "ella_invitation_capacity_reservations",
                "ella_invitations",
                "ella_invitation_targets",
            ),
        ),
        (
            "012_create_account_profile_runtime_targets.sql",
            (
                "ella_runtime_targets",
                "ella_runtime_session_scopes",
                "ella_runtime_interactions",
            ),
        ),
        (
            "013_create_managed_cloud_consent_authority.sql",
            ("ella_managed_cloud_consent_authority",),
        ),
    ],
)
def test_migration_prerequisite_failure_is_atomic(filename, leaked_relations):
    asyncio.run(_assert_failure_rolls_back(filename, leaked_relations))
