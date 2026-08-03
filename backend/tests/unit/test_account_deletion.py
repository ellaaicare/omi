import asyncio
import ast
from pathlib import Path
from types import SimpleNamespace

from fastapi import Depends, Response
from database import account_deletion as account_deletion_db
from database.firestore_account_deletion import delete_firestore_user_data
from ella.services import account_deletion as account_deletion_service
from firebase_admin.auth import UserNotFoundError
from utils.other import endpoints as auth_endpoints


class _Snapshot:
    def __init__(self, reference):
        self.reference = reference


class _Document:
    def __init__(self, name, *, exists=True, children=None):
        self.name = name
        self.exists = exists
        self.deleted = False
        self._children = list(children or ())

    def get(self):
        return type("DocumentState", (), {"exists": self.exists and not self.deleted})()

    def collections(self):
        return list(self._children)

    def delete(self):
        self.deleted = True


class _Collection:
    def __init__(self, documents=None):
        self.documents = list(documents or ())

    def limit(self, _batch_size):
        return self

    def stream(self):
        return [_Snapshot(document) for document in self.documents if not document.deleted]


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
        for reference in self.references:
            reference.delete()


class _Firestore:
    def __init__(self, user_document):
        self.user_document = user_document
        self.fail_next_commit = False

    def collection(self, name):
        assert name == "users"
        return self

    def document(self, _uid):
        return self.user_document

    def batch(self):
        return _Batch(self)


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
    child_document = _Document("child", children=[_Collection([nested_document])])
    root_document = _Document(
        "user",
        children=[_Collection([]), _Collection([child_document])],
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
    class Pool:
        async def fetchval(self, _query, uid):
            assert uid == "retained-firebase-subject"
            return "DELETION_PENDING"

    async def pool():
        return Pool()

    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "true")
    monkeypatch.setattr(auth_endpoints.voice_canary, "get_pool", pool)

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

    monkeypatch.setenv("ELLA_POSTGRES_AUTHORITY_ENABLED", "false")
    monkeypatch.setattr(auth_endpoints.voice_canary, "get_pool", forbidden_pool)

    assert (
        asyncio.run(auth_endpoints.assert_authenticated_user_writable("legacy-firebase-subject"))
        == "legacy-firebase-subject"
    )
