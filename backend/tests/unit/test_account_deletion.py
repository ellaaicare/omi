import asyncio
import ast
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, Response
import pytest
from database import account_deletion as account_deletion_db
from database import firestore_account_deletion
from database.firestore_account_deletion import FirestoreAccountDeletionIncomplete, delete_firestore_user_data
from ella.services import account_deletion as account_deletion_service
from firebase_admin.auth import UserNotFoundError
from utils.other import endpoints as auth_endpoints


@pytest.fixture(autouse=True)
def _successful_content_fence(monkeypatch):
    async def tombstone(_uid):
        return True

    async def purge_routing_traces(_uid):
        return 0

    async def purge_memory_reinterpretation_work(_uid):
        return 0

    async def purge_canonical_event_ledger(_uid):
        return 0

    monkeypatch.setattr(
        account_deletion_service.content_write_fence,
        "tombstone_content_writes",
        tombstone,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_routing_traces",
        purge_routing_traces,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_memory_reinterpretation_work",
        purge_memory_reinterpretation_work,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_canonical_event_ledger",
        purge_canonical_event_ledger,
    )


class _Snapshot:
    def __init__(self, reference):
        self.reference = reference

    @property
    def id(self):
        return self.reference.name

    def to_dict(self):
        return dict(self.reference.data)


class _Document:
    def __init__(self, name, *, exists=True, children=None, data=None):
        self.name = name
        self.exists = exists
        self.deleted = False
        self._children = list(children or ())
        self.data = dict(data or {})

    def get(self):
        return type(
            "DocumentState",
            (),
            {
                "exists": self.exists and not self.deleted,
                "to_dict": lambda _self: dict(self.data),
            },
        )()

    def to_dict(self):
        return dict(self.data)

    def collections(self):
        return list(self._children)

    def delete(self):
        self.deleted = True


class _Query:
    def __init__(self, documents=None, *, limit=None):
        self.documents = list(documents or ())
        self._limit = limit

    def limit(self, batch_size):
        return _Query(self.documents, limit=batch_size)

    def where(self, *, filter):
        return _Query(
            [document for document in self.documents if document.data.get(filter.field_path) == filter.value],
            limit=self._limit,
        )

    def stream(self):
        documents = [document for document in self.documents if document.exists and not document.deleted]
        if self._limit is not None:
            documents = documents[: self._limit]
        return [_Snapshot(document) for document in documents]


class _Collection(_Query):
    def __init__(self, name, documents=None):
        super().__init__(documents)
        self.id = name

    def document(self, document_id):
        for document in self.documents:
            if document.name == document_id:
                return document
        document = _Document(document_id, exists=False)
        self.documents.append(document)
        return document


class _Batch:
    def __init__(self, database):
        self.database = database
        self.references = []

    def delete(self, reference):
        self.references.append(reference)

    def commit(self):
        if self.database.fail_next_commit:
            self.database.fail_next_commit = False
            raise RuntimeError("synthetic_firestore_failure")
        self.database.committed_batch_sizes.append(len(self.references))
        for reference in self.references:
            reference.delete()


class _Firestore:
    def __init__(self, user_document, *, collections=None, collection_groups=None):
        self._collections = {name: _Collection(name, documents) for name, documents in (collections or {}).items()}
        self._collections["users"] = _Collection("users", [user_document])
        self._collection_groups = {
            name: _Collection(name, documents) for name, documents in (collection_groups or {}).items()
        }
        self.fail_next_commit = False
        self.committed_batch_sizes = []

    def collection(self, name):
        if name not in self._collections:
            self._collections[name] = _Collection(name)
        return self._collections[name]

    def collection_group(self, name):
        if name not in self._collection_groups:
            self._collection_groups[name] = _Collection(name)
        return self._collection_groups[name]

    def collections(self):
        return list(self._collections.values())

    def batch(self):
        return _Batch(self)


class _CacheAuthority:
    def __init__(self, *, developer=(), mcp=(), retain=False):
        self.developer = set(developer)
        self.mcp = set(mcp)
        self.retain = retain
        self.calls = []

    def invalidate_cached_dev_api_key(self, hashed_key):
        self.calls.append(("developer", hashed_key))
        if not self.retain:
            self.developer.discard(hashed_key)
        return hashed_key not in self.developer

    def invalidate_cached_mcp_api_key(self, hashed_key):
        self.calls.append(("mcp", hashed_key))
        if not self.retain:
            self.mcp.discard(hashed_key)
        return hashed_key not in self.mcp


def _state(*, external=()):
    return account_deletion_db.AccountDeletionState(
        user_found=True,
        capacity_released=True,
        authority_quarantined=True,
        external_cleanup_required=tuple(external),
        external_cleanup_references=(),
        counts={},
    )


def _load_production_delete_route():
    path = Path(__file__).resolve().parents[2] / "routers" / "users.py"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    route = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_account")
    route.decorator_list = []
    namespace = {
        "Depends": Depends,
        "Response": Response,
        "auth": SimpleNamespace(
            get_authenticated_user_uid=lambda: "unused",
            delete_account=lambda _uid: None,
        ),
        "delete_user_data": lambda _uid: None,
        "execute_account_deletion": account_deletion_service.execute_account_deletion,
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=[route], type_ignores=[])), str(path), "exec"), namespace)
    return namespace["delete_account"], namespace


def test_authenticated_route_delegates_to_the_resumable_deletion_service():
    path = Path(__file__).resolve().parents[2] / "routers" / "users.py"
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    route = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "delete_account")
    route_source = ast.get_source_segment(source, route) or ""

    assert "@router.delete('/v1/users/delete-account'" in source
    assert "await execute_account_deletion(" in route_source
    assert "delete_firestore=delete_user_data" in route_source
    assert "delete_firebase=auth.delete_account" in route_source
    assert "except Exception" not in route_source


def test_firestore_delete_is_idempotent_and_resumes_after_partial_failure():
    nested_document = _Document("nested")
    child_document = _Document("child", children=[_Collection("nested-items", [nested_document])])
    root_document = _Document(
        "synthetic-user",
        children=[_Collection("empty"), _Collection("children", [child_document])],
    )
    firestore = _Firestore(root_document)
    firestore.fail_next_commit = True
    try:
        delete_firestore_user_data(firestore, "synthetic-user")
    except RuntimeError as exc:
        assert str(exc) == "synthetic_firestore_failure"
    else:  # pragma: no cover - the fake must exercise the failure boundary
        raise AssertionError("expected the synthetic batch failure")

    result = delete_firestore_user_data(firestore, "synthetic-user")
    replay = delete_firestore_user_data(firestore, "synthetic-user")

    assert result == {
        "status": "ok",
        "message": "Account data deleted successfully",
        "documents_deleted": 3,
    }
    assert replay["status"] == "ok"
    assert replay["documents_deleted"] == 0


def test_firestore_inventory_snapshot_covers_every_repository_owned_root():
    assert tuple(
        (owned.name, owned.owner_field, owned.api_key_cache)
        for owned in firestore_account_deletion.OWNED_TOP_LEVEL_COLLECTIONS
    ) == (
        ("analytics", "uid", None),
        ("dev_api_keys", "user_id", "developer"),
        ("mcp_api_keys", "user_id", "mcp"),
        ("tasks", "user_uid", None),
        ("import_jobs", "uid", None),
        ("ella_hermes_cloud_enrichment_outbox", "uid", None),
        ("plugins_data", "uid", None),
        ("mcp_identity_grants", "profile_uid", None),
    )
    assert tuple((owned.name, owned.owner_field) for owned in firestore_account_deletion.OWNED_COLLECTION_GROUPS) == (
        ("reviews", "uid"),
        ("usage_history", "uid"),
    )
    assert firestore_account_deletion.DIRECT_UID_DOCUMENT_COLLECTIONS == ("testers",)


def test_production_firestore_cleanup_removes_seeded_inventory_and_retains_other_uid():
    uid = "synthetic-user"
    other_uid = "other-user"
    ownership = {
        "analytics": ("uid", None),
        "dev_api_keys": ("user_id", "dev-owned-hash"),
        "mcp_api_keys": ("user_id", "mcp-owned-hash"),
        "tasks": ("user_uid", None),
        "import_jobs": ("uid", None),
        "ella_hermes_cloud_enrichment_outbox": ("uid", None),
        "plugins_data": ("uid", None),
        "mcp_identity_grants": ("profile_uid", None),
    }
    documents = {}
    owned_documents = []
    other_documents = []
    for collection_name, (owner_field, hashed_key) in ownership.items():
        owned_data = {owner_field: uid}
        other_data = {owner_field: other_uid}
        if hashed_key:
            owned_data["hashed_key"] = hashed_key
            other_data["hashed_key"] = f"other-{hashed_key}"
        owned = _Document(f"owned-{collection_name}", data=owned_data)
        other = _Document(f"other-{collection_name}", data=other_data)
        documents[collection_name] = [owned, other]
        owned_documents.append(owned)
        other_documents.append(other)

    tester_owned = _Document(uid, data={"uid": uid})
    tester_other = _Document(other_uid, data={"uid": other_uid})
    documents["testers"] = [tester_owned, tester_other]
    owned_review = _Document("owned-review", data={"uid": uid})
    other_review = _Document("other-review", data={"uid": other_uid})
    owned_usage = _Document("owned-usage", data={"uid": uid})
    other_usage = _Document("other-usage", data={"uid": other_uid})
    nested = _Document("nested")
    child = _Document("child", children=[_Collection("nested", [nested])])
    root_document = _Document(uid, children=[_Collection("children", [child])])
    cache = _CacheAuthority(
        developer=("dev-owned-hash", "other-dev-owned-hash"),
        mcp=("mcp-owned-hash", "other-mcp-owned-hash"),
    )
    firestore = _Firestore(
        root_document,
        collections=documents,
        collection_groups={
            "reviews": [owned_review, other_review],
            "usage_history": [owned_usage, other_usage],
        },
    )

    result = delete_firestore_user_data(firestore, uid, batch_size=2, cache_authority=cache)
    replay = delete_firestore_user_data(firestore, uid, batch_size=2, cache_authority=cache)

    assert result["documents_deleted"] == 14
    assert replay["documents_deleted"] == 0
    assert all(document.deleted for document in owned_documents)
    assert all(not document.deleted for document in other_documents)
    assert tester_owned.deleted is True
    assert tester_other.deleted is False
    assert owned_review.deleted is True
    assert other_review.deleted is False
    assert owned_usage.deleted is True
    assert other_usage.deleted is False
    assert child.deleted is True
    assert nested.deleted is True
    assert root_document.deleted is True
    assert cache.developer == {"other-dev-owned-hash"}
    assert cache.mcp == {"other-mcp-owned-hash"}
    assert cache.calls == [
        ("developer", "dev-owned-hash"),
        ("developer", "dev-owned-hash"),
        ("mcp", "mcp-owned-hash"),
        ("mcp", "mcp-owned-hash"),
    ]


def test_firestore_owned_collection_purge_is_bounded_across_multiple_batches():
    uid = "synthetic-user"
    tasks = [_Document(f"task-{index}", data={"user_uid": uid}) for index in range(5)]
    firestore = _Firestore(_Document(uid), collections={"tasks": tasks})

    result = delete_firestore_user_data(
        firestore,
        uid,
        batch_size=2,
        cache_authority=_CacheAuthority(),
    )

    assert result["documents_deleted"] == 6
    assert firestore.committed_batch_sizes == [2, 2, 1]
    assert all(task.deleted for task in tasks)


@pytest.mark.parametrize("omitted_collection", ["analytics", "tasks"])
def test_firestore_gate_fails_closed_when_known_inventory_entry_is_omitted(monkeypatch, omitted_collection):
    uid = "synthetic-user"
    owner_field = "uid" if omitted_collection == "analytics" else "user_uid"
    retained = _Document("retained", data={owner_field: uid})
    root_document = _Document(uid)
    monkeypatch.setattr(
        firestore_account_deletion,
        "OWNED_TOP_LEVEL_COLLECTIONS",
        tuple(
            owned
            for owned in firestore_account_deletion.OWNED_TOP_LEVEL_COLLECTIONS
            if owned.name != omitted_collection
        ),
    )
    firestore = _Firestore(root_document, collections={omitted_collection: [retained]})

    with pytest.raises(
        FirestoreAccountDeletionIncomplete,
        match="account_deletion_firestore_inventory_incomplete",
    ):
        delete_firestore_user_data(firestore, uid, cache_authority=_CacheAuthority())

    assert retained.deleted is False


def test_firestore_gate_positive_control_detects_new_uid_linked_top_level_collection():
    uid = "synthetic-user"
    retained = _Document("retained", data={"uid": uid})
    other = _Document("other", data={"uid": "other-user"})
    firestore = _Firestore(
        _Document(uid),
        collections={"new_uid_linked_collection": [retained, other]},
    )

    with pytest.raises(
        FirestoreAccountDeletionIncomplete,
        match="account_deletion_firestore_inventory_incomplete",
    ):
        delete_firestore_user_data(firestore, uid, cache_authority=_CacheAuthority())

    assert retained.deleted is False
    assert other.deleted is False


def test_firestore_api_key_cache_failure_preserves_resumable_key_record():
    uid = "synthetic-user"
    key = _Document("owned-key", data={"user_id": uid, "hashed_key": "owned-hash"})
    firestore = _Firestore(_Document(uid), collections={"dev_api_keys": [key]})

    with pytest.raises(
        FirestoreAccountDeletionIncomplete,
        match="account_deletion_api_key_cache_retained",
    ):
        delete_firestore_user_data(
            firestore,
            uid,
            cache_authority=_CacheAuthority(developer=("owned-hash",), retain=True),
        )

    assert key.deleted is False


def test_deletion_service_returns_typed_pending_and_preserves_auth_retry(monkeypatch):
    calls = []

    async def quarantine(_uid):
        calls.append("quarantine")
        return _state(external=("hermes_profile", "honcho_tenancy", "runtime_registry"))

    async def forbidden_finalize(_uid):
        raise AssertionError("external cleanup must precede finalization")

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        quarantine,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "finalize_account_deletion",
        forbidden_finalize,
    )
    result = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: calls.append("firestore"),
            delete_firebase=lambda _uid: calls.append("firebase"),
        )
    )

    assert result.status_code == 202
    assert result.body["status"] == "deletion_pending"
    assert result.body["authority_quarantined"] is True
    assert result.body["capacity_released"] is True
    assert result.body["deletion_receipt"]["operator_action_required"] is True
    assert calls == ["quarantine", "firestore"]


def test_deletion_service_completes_missing_external_profile_and_is_repeatable(monkeypatch):
    calls = []

    async def quarantine(_uid):
        calls.append("quarantine")
        return _state()

    async def finalize(_uid):
        calls.append("finalize")
        return True

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        quarantine,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "finalize_account_deletion",
        finalize,
    )
    for _attempt in range(2):
        result = asyncio.run(
            account_deletion_service.execute_account_deletion(
                "synthetic-user",
                delete_firestore=lambda _uid: calls.append("firestore"),
                delete_firebase=lambda _uid: calls.append("firebase"),
            )
        )
        assert result.body["status"] == "ok"

    assert calls == [
        "quarantine",
        "firestore",
        "finalize",
        "firebase",
        "quarantine",
        "firestore",
        "finalize",
        "firebase",
    ]


def test_deletion_service_converts_firestore_and_firebase_failures_to_resumable_state(monkeypatch):
    async def quarantine(_uid):
        return _state()

    async def finalize(_uid):
        return True

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        quarantine,
    )
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "finalize_account_deletion",
        finalize,
    )
    firestore_pending = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: (_ for _ in ()).throw(RuntimeError("content must not escape")),
            delete_firebase=lambda _uid: None,
        )
    )
    assert firestore_pending.status_code == 202
    assert firestore_pending.body["deletion_receipt"]["remaining"] == ["firestore_data"]

    firebase_pending = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: None,
            delete_firebase=lambda _uid: (_ for _ in ()).throw(RuntimeError("content must not escape")),
        )
    )
    assert firebase_pending.status_code == 202
    assert firebase_pending.body["deletion_receipt"]["remaining"] == ["firebase_identity"]

    retry_calls = []

    async def quarantine_with_firestore_retry(_uid):
        return _state(external=("firestore_data",))

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        quarantine_with_firestore_retry,
    )
    retry = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: retry_calls.append("firestore"),
            delete_firebase=lambda _uid: retry_calls.append("firebase"),
        )
    )
    assert retry.status_code == 200
    assert retry_calls == ["firestore", "firebase"]


def test_deletion_service_never_reports_success_when_routing_trace_purge_is_unavailable(monkeypatch):
    calls = []

    async def quarantine(_uid):
        return _state()

    async def unavailable(_uid):
        raise account_deletion_db.AccountDeletionUnavailable("account_deletion_routing_traces_unavailable")

    async def forbidden_finalize(_uid):
        raise AssertionError("trace absence must precede finalization")

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "purge_routing_traces", unavailable)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", forbidden_finalize)

    result = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda uid: calls.append(("firestore", uid)),
            delete_firebase=lambda uid: calls.append(("firebase", uid)),
        )
    )

    assert result.status_code == 202
    assert result.body["deletion_receipt"]["remaining"] == ["routing_traces"]
    assert calls == [("firestore", "synthetic-user")]


def test_deletion_service_retains_firebase_when_memory_work_purge_is_unavailable(monkeypatch):
    calls = []

    async def quarantine(_uid):
        return _state()

    async def unavailable(_uid):
        raise account_deletion_db.AccountDeletionUnavailable("account_deletion_memory_reinterpretation_unavailable")

    async def forbidden_finalize(_uid):
        raise AssertionError("memory-work absence must precede finalization")

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "purge_memory_reinterpretation_work",
        unavailable,
    )
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", forbidden_finalize)

    result = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda uid: calls.append(("firestore", uid)),
            delete_firebase=lambda uid: calls.append(("firebase", uid)),
        )
    )

    assert result.status_code == 202
    assert result.body["deletion_receipt"]["remaining"] == ["memory_reinterpretation"]
    assert calls == [("firestore", "synthetic-user")]


def test_canonical_ledger_purge_is_resumable_and_precedes_firebase_last(monkeypatch):
    calls = []

    async def quarantine(_uid):
        return _state()

    async def unavailable(_uid):
        calls.append("canonical-purge-failed")
        raise account_deletion_db.AccountDeletionUnavailable("account_deletion_canonical_event_ledger_unavailable")

    async def purged(_uid):
        calls.append("canonical-purged")
        return 2

    async def finalize(_uid):
        calls.append("finalized")
        return True

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "quarantine_account_for_deletion", quarantine)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "purge_canonical_event_ledger", unavailable)
    monkeypatch.setattr(account_deletion_service.account_deletion_db, "finalize_account_deletion", finalize)

    pending = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: calls.append("firestore"),
            delete_firebase=lambda _uid: calls.append("firebase"),
        )
    )
    assert pending.status_code == 202
    assert pending.body["deletion_receipt"]["remaining"] == ["canonical_event_ledger"]
    assert calls == ["canonical-purge-failed", "firestore"]

    monkeypatch.setattr(account_deletion_service.account_deletion_db, "purge_canonical_event_ledger", purged)
    completed = asyncio.run(
        account_deletion_service.execute_account_deletion(
            "synthetic-user",
            delete_firestore=lambda _uid: calls.append("firestore-retry"),
            delete_firebase=lambda _uid: calls.append("firebase"),
        )
    )
    assert completed.status_code == 200
    assert calls[-4:] == ["canonical-purged", "firestore-retry", "finalized", "firebase"]


def test_firebase_delete_lost_ack_converges_only_after_authoritative_absence(monkeypatch):
    monkeypatch.setattr(
        auth_endpoints.auth,
        "delete_user",
        lambda _uid: (_ for _ in ()).throw(RuntimeError("synthetic_lost_ack")),
    )
    monkeypatch.setattr(
        auth_endpoints.auth,
        "get_user",
        lambda _uid: (_ for _ in ()).throw(UserNotFoundError("not found")),
    )

    assert auth_endpoints.delete_account("synthetic-user") == {"status": "already_deleted"}


def test_production_route_uses_legacy_resumable_deletion_when_ella_authority_is_explicitly_disabled(monkeypatch):
    calls = []
    monkeypatch.setenv("ELLA_ENABLED", "false")
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")

    async def forbidden_quarantine(_uid):
        raise AssertionError("disabled Ella persistence must not be probed")

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        forbidden_quarantine,
    )
    route, namespace = _load_production_delete_route()
    namespace["delete_user_data"] = lambda uid: calls.append(("firestore", uid))
    namespace["auth"].delete_account = lambda uid: calls.append(("firebase", uid))
    response = Response()
    body = asyncio.run(route(response=response, uid="authenticated-user"))

    assert response.status_code == 200
    assert body["status"] == "ok"
    assert calls == [("firestore", "authenticated-user"), ("firebase", "authenticated-user")]


def test_production_route_fails_closed_before_destructive_work_when_enabled_authority_is_unavailable(monkeypatch):
    calls = []
    monkeypatch.setenv("ELLA_ENABLED", "true")
    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true")

    async def unavailable(_uid):
        raise account_deletion_db.AccountDeletionUnavailable("account_deletion_authority_unavailable")

    monkeypatch.setattr(
        account_deletion_service.account_deletion_db,
        "quarantine_account_for_deletion",
        unavailable,
    )
    route, namespace = _load_production_delete_route()
    namespace["delete_user_data"] = lambda uid: calls.append(("firestore", uid))
    namespace["auth"].delete_account = lambda uid: calls.append(("firebase", uid))
    try:
        asyncio.run(
            route(
                response=Response(),
                uid="authenticated-user",
            )
        )
    except Exception as exc:
        assert exc.status_code == 503
        assert exc.detail == {
            "code": "account_deletion_authority_unavailable",
            "retryable": True,
        }
    else:
        raise AssertionError("enabled unavailable authority must fail closed")
    assert calls == []


def test_retained_firebase_subject_is_fenced_from_authenticated_content_writes(monkeypatch):
    @asynccontextmanager
    async def tombstoned_fence(uid):
        assert uid == "retained-firebase-subject"
        raise account_deletion_service.content_write_fence.ContentWriteFenceError("account_write_forbidden")
        yield  # pragma: no cover

    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr(
        auth_endpoints.content_write_fence,
        "content_write_fence",
        tombstoned_fence,
    )

    try:
        asyncio.run(auth_endpoints.assert_authenticated_user_writable("retained-firebase-subject"))
    except Exception as exc:
        assert exc.status_code == 403
        assert exc.detail == {
            "code": "account_write_forbidden",
            "retryable": False,
        }
    else:
        raise AssertionError("tombstoned Firebase subject must not regain content write authority")

    monkeypatch.setenv("FIRESTORE_EMULATOR_HOST", "localhost:9999")
    monkeypatch.setenv("GCLOUD_PROJECT", "omi-ci")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "omi-ci")

    from fastapi import FastAPI, HTTPException
    from fastapi.testclient import TestClient
    from routers import announcements

    calls = []

    async def tombstoned_subject():
        raise HTTPException(
            status_code=403,
            detail={"code": "account_write_forbidden", "retryable": False},
        )

    monkeypatch.setattr(
        announcements,
        "get_announcement_by_id",
        lambda _announcement_id: calls.append("lookup"),
    )
    monkeypatch.setattr(
        announcements,
        "dismiss_announcement",
        lambda *_args, **_kwargs: calls.append("firestore"),
    )
    app = FastAPI()
    app.include_router(announcements.router)
    app.dependency_overrides[auth_endpoints.get_writable_user_uid] = tombstoned_subject

    response = TestClient(app).post(
        "/v1/announcements/synthetic-announcement/dismiss",
        json={"cta_clicked": False},
    )

    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "account_write_forbidden"
    assert calls == []


def test_explicit_legacy_mode_does_not_probe_optional_ella_authority_for_content_writes(monkeypatch):
    async def forbidden_pool():
        raise AssertionError("explicitly disabled Ella authority must not be probed")

    @asynccontextmanager
    async def allowed_fence(uid):
        assert uid == "legacy-firebase-subject"
        yield uid

    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(
        account_deletion_service.content_write_fence.voice_canary,
        "get_pool",
        forbidden_pool,
    )
    monkeypatch.setattr(auth_endpoints.content_write_fence, "content_write_fence", allowed_fence)

    assert (
        asyncio.run(auth_endpoints.assert_authenticated_user_writable("legacy-firebase-subject"))
        == "legacy-firebase-subject"
    )
