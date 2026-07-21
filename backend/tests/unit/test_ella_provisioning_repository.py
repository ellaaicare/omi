import asyncio
import uuid

from database.ella_provisioning import EllaProvisioningRepository


class _AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _Connection:
    def __init__(self):
        self.queries = []

    def transaction(self):
        return _AsyncContext(self)

    async def fetchrow(self, query, *args):
        self.queries.append(query)
        if "INSERT INTO users" not in query:
            return None

        assert "$5::text" in query
        assert "$2::text" in query
        return {
            "id": args[0],
            "omi_uid": args[4],
            "email": args[1],
            "name": args[2],
            "timezone": args[3],
            "status": "PENDING",
        }


class _Pool:
    def __init__(self, connection):
        self.connection = connection

    def acquire(self):
        return _AsyncContext(self.connection)


def test_fresh_identity_insert_casts_jsonb_parameters():
    connection = _Connection()
    repository = EllaProvisioningRepository(_Pool(connection))

    result = asyncio.run(
        repository.ensure_user_identity(
            uid="firebase-user-1",
            email="user@example.com",
            name="Test User",
            timezone_name="America/Los_Angeles",
        )
    )

    assert result == {
        "id": result["id"],
        "omi_uid": "firebase-user-1",
        "email": "user@example.com",
        "name": "Test User",
        "timezone": "America/Los_Angeles",
        "status": "PENDING",
    }
    assert isinstance(result["id"], uuid.UUID)
    assert len(connection.queries) == 3
