import os
import subprocess
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]


def _run_clean_backend_probe(source: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(BACKEND_ROOT)
    return subprocess.run(
        [sys.executable, "-c", source],
        cwd=BACKEND_ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_runtime_resolver_imports_real_in_tree_dependency_closure():
    probe = _run_clean_backend_probe(
        "from ella.services.runtime_resolver import "
        "_self_hosted_target_mode, cloud_runtime_authority_identity, revalidate_cloud_runtime_authority; "
        "assert _self_hosted_target_mode('hermes-cloud-transcript') == 'hermes-chat'; "
        "print('runtime-import-ok')"
    )

    assert probe.returncode == 0, probe.stderr
    assert probe.stdout.strip() == "runtime-import-ok"


def test_main_wires_consent_firestore_and_router_while_disabled_extensions_stay_fail_closed():
    probe = _run_clean_backend_probe(r'''
import os
import sys
from types import ModuleType, SimpleNamespace

os.environ["ELLA_ENABLED"] = "false"
os.environ["ELLA_AI_CONSENT_ENFORCEMENT_ENABLED"] = "false"

fake_db = object()
database_client = ModuleType("database._client")
database_client.db = fake_db
sys.modules["database._client"] = database_client

firebase_admin = ModuleType("firebase_admin")
firebase_admin.credentials = SimpleNamespace(Certificate=lambda value: value)
firebase_admin.auth = SimpleNamespace(verify_id_token=lambda value: {})
firebase_admin.initialize_app = lambda *args, **kwargs: None
sys.modules["firebase_admin"] = firebase_admin

class _Decorator:
    def __call__(self, *args, **kwargs):
        return lambda function: function

class _Image:
    @classmethod
    def debian_slim(cls):
        return cls()
    def apt_install(self, *args):
        return self
    def pip_install_from_requirements(self, *args):
        return self

class _App:
    def __init__(self, *args, **kwargs):
        pass
    function = _Decorator()

modal = ModuleType("modal")
modal.Image = _Image
modal.App = _App
modal.asgi_app = _Decorator()
modal.Secret = SimpleNamespace(from_name=lambda value: value)
sys.modules["modal"] = modal

from fastapi import APIRouter
routers = ModuleType("routers")
for name in (
    "workflow", "chat", "firmware", "plugins", "transcribe", "notifications",
    "speech_profile", "agents", "users", "trends", "sync", "apps", "custom_auth",
    "payment", "integration", "conversations", "memories", "mcp", "mcp_sse", "oauth",
    "auth", "action_items", "task_integrations", "integrations", "other", "developer",
    "updates", "calendar_meetings", "imports", "knowledge_graph", "wrapped", "folders",
    "goals", "announcements",
):
    setattr(routers, name, SimpleNamespace(router=APIRouter()))
sys.modules["routers"] = routers

observability = ModuleType("utils.observability")
observability.log_langsmith_status = lambda: None
sys.modules["utils.observability"] = observability

import main
from ella.services import ai_consent
from fastapi import HTTPException

assert ai_consent._firestore_db is fake_db
assert "/v1/users/ai-consent" in {route.path for route in main.app.routes}

class _NoGrantRepository:
    def get_current(self, uid):
        return None, None
    def get_state(self, uid):
        return None
    def get_receipt(self, uid, receipt_id):
        return None

ai_consent._repository = _NoGrantRepository()
try:
    ai_consent.require_current_ai_consent("owner-probe")
except HTTPException as exc:
    assert exc.status_code == 403
    assert exc.detail["code"] == "ai_consent_required"
else:
    raise AssertionError("disabled rollout bypassed correction consent")
print("consent-startup-ok")
''')

    assert probe.returncode == 0, probe.stderr
    assert "consent-startup-ok" in probe.stdout
