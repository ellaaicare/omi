import json
import os

import firebase_admin
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from database._client import db as firestore_db
from modal import Image, App, asgi_app, Secret
from routers import (
    workflow,
    chat,
    firmware,
    plugins,
    transcribe,
    notifications,
    speech_profile,
    agents,
    users,
    trends,
    sync,
    apps,
    custom_auth,
    payment,
    integration,
    conversations,
    memories,
    mcp,
    mcp_sse,
    oauth,
    auth,
    action_items,
    task_integrations,
    integrations,
    other,
    developer,
    updates,
    calendar_meetings,
    imports,
    knowledge_graph,
    wrapped,
    folders,
    goals,
    announcements,
)

from utils.other.timeout import TimeoutMiddleware
from utils.observability import log_langsmith_status
from ella.routers import ai_consent
from ella.services.ai_consent import configure_firestore_db as configure_ai_consent_firestore_db

# Log LangSmith tracing status at startup
log_langsmith_status()

if os.environ.get('SERVICE_ACCOUNT_JSON'):
    service_account_info = json.loads(os.environ["SERVICE_ACCOUNT_JSON"])
    credentials = firebase_admin.credentials.Certificate(service_account_info)
    firebase_admin.initialize_app(credentials)
else:
    firebase_admin.initialize_app()

app = FastAPI()

# CORS — allow admin dashboard + localhost dev
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://admin.ella-ai-care.com",
        "https://ella-ai-care.com",
        "https://www.ella-ai-care.com",
        "http://localhost:3000",
        "http://localhost:3002",
    ],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
    max_age=3600,
)

# Consent authority remains wired even when Ella extensions are disabled so
# protected generic OMI routes cannot outlive their grant/revoke API.
configure_ai_consent_firestore_db(firestore_db)
app.include_router(ai_consent.router)
app.include_router(transcribe.router)
app.include_router(conversations.router)
app.include_router(action_items.router)
app.include_router(task_integrations.router)
app.include_router(integrations.router)
app.include_router(memories.router)
app.include_router(chat.router)
app.include_router(plugins.router)
app.include_router(speech_profile.router)
# app.include_router(screenpipe.router)
app.include_router(notifications.router)
app.include_router(workflow.router)
app.include_router(integration.router)
app.include_router(agents.router)
app.include_router(users.router)
app.include_router(trends.router)

app.include_router(other.router)

app.include_router(firmware.router)
app.include_router(updates.router)
app.include_router(sync.router)

app.include_router(apps.router)
app.include_router(custom_auth.router)
app.include_router(calendar_meetings.router)
app.include_router(oauth.router)  # Added oauth router (for Omi Apps)
app.include_router(auth.router)  # Added auth router (for the main Omi App, this is the core auth router)


app.include_router(payment.router)
app.include_router(mcp.router)
app.include_router(mcp_sse.router)
app.include_router(developer.router)
app.include_router(imports.router)
app.include_router(wrapped.router)
app.include_router(folders.router)
app.include_router(knowledge_graph.router)
app.include_router(goals.router)
app.include_router(announcements.router)


methods_timeout = {
    "GET": os.environ.get('HTTP_GET_TIMEOUT'),
    "PUT": os.environ.get('HTTP_PUT_TIMEOUT'),
    "PATCH": os.environ.get('HTTP_PATCH_TIMEOUT'),
    "DELETE": os.environ.get('HTTP_DELETE_TIMEOUT'),
}

app.add_middleware(TimeoutMiddleware, methods_timeout=methods_timeout)


modal_app = App(
    name='backend',
    secrets=[Secret.from_name("gcp-credentials"), Secret.from_name('envs')],
)
image = Image.debian_slim().apt_install('ffmpeg', 'git', 'unzip').pip_install_from_requirements('requirements.txt')


@modal_app.function(
    image=image,
    keep_warm=0,
    memory=(512, 1024),
    cpu=2,
    allow_concurrent_inputs=10,
    timeout=60 * 10,
)
@asgi_app()
def api():
    return app


paths = ['_temp', '_samples', '_segments', '_speech_profiles']
for path in paths:
    if not os.path.exists(path):
        os.makedirs(path)

# ============================================================================
# ELLA EXTENSIONS (downstream fork customizations)
# ============================================================================
_ella_extensions_required = os.getenv("ELLA_ENABLED", "true").lower() == "true"
try:
    from ella import register_ella_extensions

    register_ella_extensions(app)
except ImportError:
    if _ella_extensions_required:
        raise
if _ella_extensions_required:
    _registered_route_paths = {route.path for route in app.routes}
    _required_correction_routes = {
        "/v1/conversations/{conversation_id}/corrections",
        "/v1/ella/conversations/{conversation_id}/corrections",
        "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}",
        "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/retry",
        "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo",
    }
    _missing_correction_routes = _required_correction_routes - _registered_route_paths
    if _missing_correction_routes:
        raise RuntimeError(f"Ella correction routes missing at startup: {sorted(_missing_correction_routes)}")
