import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]
ROUTERS = BACKEND / "ella" / "routers"
HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "WEBSOCKET"}


def _group(module, authority, disposition, *routes):
    return module, authority, disposition, routes


ROUTE_GROUPS = (
    _group(
        "ai_consent",
        "public_metadata",
        "public",
        ("GET", "/v1/users/ai-consent/policy", "get_ai_consent_policy"),
    ),
    _group(
        "ai_consent",
        "firebase_exact_owner",
        "public",
        ("GET", "/v1/users/ai-consent", "get_ai_consent_status"),
        ("GET", "/v1/users/ai-consent/receipts/{receipt_id}", "get_ai_consent_receipt"),
        ("POST", "/v1/users/ai-consent", "submit_ai_consent"),
    ),
    _group(
        "callbacks",
        "callback_service_exact_subject",
        "staged_public",
        ("PATCH", "/v1/ella/conversation/{conversation_id}/summary", "update_conversation_summary"),
        (
            "GET",
            "/v1/ella/conversations/enrichment/reconcile-candidates",
            "list_enrichment_reconcile_candidates",
        ),
        ("GET", "/v1/ella/conversation/{conversation_id}/data", "get_conversation_data"),
        ("POST", "/v1/ella/notification", "ella_notification"),
        ("POST", "/v1/ella/daily-summary", "ella_daily_summary"),
    ),
    _group(
        "callbacks",
        "public_metadata",
        "public",
        ("GET", "/v1/ella/health", "ella_health"),
    ),
    _group(
        "callbacks",
        "public_capability_metadata",
        "public",
        (
            "GET",
            "/v1/ella/conversation/summary/capabilities",
            "conversation_summary_capabilities",
        ),
    ),
    _group(
        "callbacks",
        "firebase_exact_owner",
        "staged_public",
        ("POST", "/v1/ella/emergency", "ella_emergency"),
        ("POST", "/v1/ella/emergency-contact", "create_emergency_contact"),
        ("GET", "/v1/ella/emergency-contacts/{uid}", "list_emergency_contacts"),
        ("PUT", "/v1/ella/emergency-contact/{contact_id}", "update_emergency_contact"),
        ("DELETE", "/v1/ella/emergency-contact/{contact_id}", "delete_emergency_contact"),
        ("GET", "/v1/ella/caregivers", "list_caregivers"),
        ("POST", "/v1/ella/caregivers/invite", "invite_caregiver"),
        ("GET", "/v1/ella/caregivers/emergency-contact", "get_emergency_caregiver"),
        ("PUT", "/v1/ella/caregivers/emergency-contact", "update_emergency_caregiver"),
        ("PUT", "/v1/ella/caregivers/{caregiver_id}/permissions", "update_caregiver_permissions"),
        ("POST", "/v1/ella/caregivers/{caregiver_id}/resend-invite", "resend_caregiver_invite"),
        ("DELETE", "/v1/ella/caregivers/{caregiver_id}", "remove_caregiver"),
    ),
    _group(
        "callbacks",
        "signed_dashboard_token",
        "public",
        ("GET", "/v1/ella/caregiver-dashboard-data", "caregiver_dashboard_data"),
    ),
    _group(
        "callbacks",
        "caregiver_service_exact_subject",
        "staged_public",
        ("POST", "/v1/ella/generate-dashboard-token", "generate_dashboard_token_endpoint"),
    ),
    _group(
        "canonical_events",
        "firebase_or_ledger_service_exact_subject",
        "staged_public",
        ("POST", "/v1/ella/events", "write_events"),
        ("POST", "/v1/ella/sessions/{session_id}/complete", "complete_session"),
        ("GET", "/v1/ella/timeline", "read_timeline"),
    ),
    _group(
        "chat",
        "firebase_exact_owner_with_consent",
        "staged_public",
        ("POST", "/v1/ella/chat/stream", "ella_chat_stream"),
    ),
    _group(
        "chat",
        "firebase_exact_owner",
        "staged_public",
        ("GET", "/v1/ella/chat/history", "ella_chat_history"),
    ),
    _group(
        "corrections",
        "firebase_exact_owner",
        "public",
        (
            "GET",
            "/v1/conversations/{conversation_id}/processing-retry-plan",
            "get_conversation_processing_retry_plan",
        ),
        (
            "POST",
            "/v1/conversations/{conversation_id}/processing-retries",
            "retry_failed_conversation_processing",
        ),
        (
            "GET",
            "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}",
            "get_conversation_correction_receipt",
        ),
        (
            "POST",
            "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo",
            "undo_conversation_correction",
        ),
    ),
    _group(
        "corrections",
        "firebase_exact_owner_with_consent",
        "public",
        (
            "POST",
            "/v1/ella/conversations/{conversation_id}/corrections",
            "submit_conversation_correction_ella",
        ),
        ("POST", "/v1/conversations/{conversation_id}/corrections", "submit_conversation_correction"),
    ),
    _group(
        "debug_metadata",
        "firebase_exact_owner",
        "permanent_edge_deny",
        ("GET", "/v1/ella/debug/conversations/metadata", "list_conversation_metadata"),
        (
            "GET",
            "/v1/ella/debug/conversations/{conversation_id}/metadata",
            "read_conversation_metadata",
        ),
    ),
    _group(
        "trace",
        "firebase_exact_owner",
        "permanent_edge_deny",
        ("POST", "/v1/ella/debug/client-trace", "ingest_client_trace"),
        ("GET", "/v1/ella/debug/traces", "get_traces"),
        ("GET", "/v1/ella/debug/trace/{uid}", "get_user_traces"),
        ("GET", "/v1/ella/debug/stats", "trace_stats"),
        ("GET", "/v1/ella/debug/status", "debug_status"),
        ("GET", "/v1/ella/debug/console", "debug_console"),
    ),
    _group(
        "escalations",
        "escalation_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/escalations/evaluate", "evaluate_escalation"),
    ),
    _group(
        "escalations",
        "firebase_or_escalation_service_exact_subject",
        "public",
        ("GET", "/v1/ella/escalations/policy", "get_escalation_policy"),
        ("GET", "/v1/ella/escalations/policy.md", "get_escalation_policy_markdown"),
    ),
    _group(
        "guardian",
        "firebase_exact_owner",
        "staged_public",
        ("GET", "/v1/ella/guardian-alerts", "guardian_alerts"),
        ("GET", "/v1/ella/guardian/mode", "get_guardian_mode"),
        ("PUT", "/v1/ella/guardian/mode", "update_guardian_mode"),
        ("GET", "/v1/ella/guardian/next-audio", "next_audio"),
        ("POST", "/v1/ella/guardian/activate", "activate_guardian"),
        ("POST", "/v1/ella/guardian/playback-event", "record_playback_event"),
        ("POST", "/v1/ella/guardian/playback-debug", "record_playback_debug_event"),
    ),
    _group(
        "guardian",
        "guardian_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/guardian/enqueue", "enqueue"),
        ("POST", "/v1/ella/guardian/synthesize", "synthesize_audio"),
        ("POST", "/v1/ella/guardian/upload", "upload_audio"),
        ("POST", "/v1/ella/guardian/upload-json", "upload_audio_json"),
        ("GET", "/v1/ella/guardian/debug-events", "view_debug_events"),
        ("POST", "/v1/ella/guardian/debug-trigger", "debug_trigger"),
        ("POST", "/v1/ella/guardian/deliver", "deliver"),
        ("POST", "/v1/ella/guardian/email/send", "email_send"),
        ("POST", "/v1/ella/guardian/trace/log", "log_pipeline_event"),
    ),
    _group(
        "guardian",
        "firebase_or_guardian_service_exact_subject",
        "internal_only",
        ("GET", "/v1/ella/guardian/queue", "view_queue"),
        ("GET", "/v1/ella/guardian/trace/{conversation_id}", "get_pipeline_trace"),
    ),
    _group(
        "hermes_cloud_enrichment",
        "hermes_enrichment_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/internal/hermes-cloud/enrichment/run", "run"),
    ),
    _group(
        "invites",
        "firebase_exact_owner",
        "public",
        ("POST", "/v1/invite/redeem", "redeem_invite"),
    ),
    _group(
        "mcp_onboarding",
        "mcp_session_exact_profile",
        "public",
        ("GET", "/v1/ella/mcp/onboarding", "get_mcp_onboarding"),
        ("GET", "/v1/ella/mcp/start_here", "get_mcp_start_here"),
        ("GET", "/v1/ella/mcp/surface-prompt", "get_mcp_surface_prompt"),
    ),
    _group(
        "mcp_onboarding",
        "firebase_exchange_exact_subject",
        "oauth_public",
        ("POST", "/v1/ella/mcp/onboarding/oauth", "post_mcp_oauth_onboarding"),
    ),
    _group(
        "mcp_onboarding",
        "oauth_protocol",
        "oauth_public",
        ("POST", "/v1/ella/mcp/register", "post_mcp_register"),
        ("GET", "/v1/ella/mcp/authorize", "get_mcp_authorize"),
        ("POST", "/v1/ella/mcp/token", "post_mcp_token"),
    ),
    _group(
        "mcp_onboarding",
        "public_metadata",
        "public",
        ("GET", "/v1/ella/mcp/surface-prompt/public", "get_public_mcp_surface_prompt"),
        ("GET", "/v1/ella/mcp/info", "get_mcp_onboarding_info"),
    ),
    _group(
        "mcp_well_known",
        "public_metadata",
        "public",
        (
            "GET",
            "/.well-known/oauth-protected-resource/v1/ella/plato/mcp",
            "get_oauth_protected_resource_subpath",
        ),
        ("GET", "/.well-known/oauth-protected-resource", "get_oauth_protected_resource"),
        ("GET", "/.well-known/oauth-authorization-server", "get_oauth_authorization_server"),
    ),
    _group(
        "memory_artwork",
        "firebase_exact_owner",
        "staged_public",
        ("GET", "/v1/ella/memory-artwork/libraries", "get_memory_artwork_libraries"),
        ("GET", "/v1/ella/memory-artwork/preferences", "get_memory_artwork_preferences"),
        ("PUT", "/v1/ella/memory-artwork/preferences", "put_memory_artwork_preferences"),
        ("GET", "/v1/ella/memories/{memory_id}/artwork", "get_memory_artwork"),
        ("POST", "/v1/ella/memories/{memory_id}/artwork", "retry_memory_artwork"),
        ("POST", "/v1/ella/memory-artwork/backfill", "backfill_memory_artwork"),
        ("POST", "/v1/ella/memory-artwork/reconciliation", "start_memory_artwork_reconciliation"),
        ("GET", "/v1/ella/memory-artwork/reconciliation", "get_memory_artwork_reconciliation"),
        ("GET", "/v1/ella/memory-artwork/queue", "get_memory_artwork_queue"),
        ("POST", "/v1/ella/memory-artwork/queue/control", "control_memory_artwork_queue"),
    ),
    _group(
        "memory_artwork",
        "memory_artwork_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/internal/memory-artwork/{memory_id}/process", "process_memory_artwork"),
    ),
    _group(
        "memory_reinterpretation",
        "firebase_exact_owner",
        "public",
        (
            "GET",
            "/v1/ella/conversations/{conversation_id}/reinterpretations/latest",
            "latest_reinterpretation",
        ),
        (
            "GET",
            "/v1/ella/conversations/{conversation_id}/reinterpretations/{job_id}",
            "get_reinterpretation",
        ),
    ),
    _group(
        "memory_reinterpretation",
        "fixed_operator",
        "internal_only",
        (
            "POST",
            "/v1/ella/internal/memory-reinterpretations/run-once",
            "run_once",
        ),
        ("GET", "/v1/ella/internal/memory-reinterpretations/metrics", "metrics"),
    ),
    _group(
        "observer",
        "observer_service_exact_subject",
        "staged_public",
        ("POST", "/v1/ella/observer/run", "run"),
        ("POST", "/v1/ella/observer/apply-pending", "apply_pending"),
        ("GET", "/v1/ella/observer/runs/{run_id}", "get_run"),
        ("GET", "/v1/ella/observer/health", "health"),
    ),
    _group(
        "onboarding",
        "firebase_exact_owner_with_consent",
        "lift_last",
        ("POST", "/v1/ella/onboarding/ensure", "ensure_onboarding"),
    ),
    _group(
        "onboarding",
        "firebase_exact_owner",
        "lift_last",
        ("GET", "/v1/ella/onboarding/status", "onboarding_status"),
    ),
    _group(
        "legacy_onboarding",
        "unauthenticated_caller_claimed_subject",
        "deprecated_legacy_public",
        ("POST", "/api/onboarding", "legacy_onboarding"),
    ),
    _group(
        "photon",
        "photon_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/internal/hermes-cloud/photon/preflight", "preflight"),
        ("POST", "/v1/ella/internal/hermes-cloud/photon/inbound", "inbound"),
        ("POST", "/v1/ella/internal/hermes-cloud/photon/delivery-ack", "delivery_ack"),
    ),
    _group(
        "plato_mcp",
        "plato_mcp_fixed_profile",
        "public",
        ("POST", "/v1/ella/plato/mcp", "plato_mcp_streamable_http"),
        ("GET", "/v1/ella/plato/mcp", "plato_mcp_sse_keepalive"),
        ("POST", "/v1/ella/plato/mcp/sse/message", "plato_mcp_sse_message"),
        ("DELETE", "/v1/ella/plato/mcp", "plato_mcp_delete_session"),
    ),
    _group(
        "plato_mcp",
        "oauth_protocol",
        "oauth_public",
        ("GET", "/v1/ella/plato/mcp/authorize", "plato_mcp_authorize"),
        ("POST", "/v1/ella/plato/mcp/token", "plato_mcp_token"),
    ),
    _group(
        "plato_mcp",
        "public_metadata",
        "public",
        ("GET", "/v1/ella/plato/mcp/info", "plato_mcp_info"),
        ("POST", "/v1/ella/plato/mcp/info", "plato_mcp_info"),
    ),
    _group(
        "resolve",
        "firebase_exact_owner",
        "public",
        ("GET", "/v1/ella/resolve", "resolve_endpoint"),
        ("GET", "/v1/ella/chat/history/{agent_id}", "proxy_chat_history"),
    ),
    _group(
        "settings",
        "firebase_exact_owner",
        "public",
        ("GET", "/v1/ella/settings", "get_settings"),
        ("PATCH", "/v1/ella/settings", "patch_settings"),
        ("GET", "/v1/ella/settings/effective", "get_effective_settings"),
        ("GET", "/v1/ella/settings/effective/voice", "get_effective_voice_settings"),
    ),
    _group(
        "today_cards",
        "firebase_exact_owner_with_consent",
        "staged_public",
        ("GET", "/v1/ella/today-card", "get_today_card"),
        ("GET", "/v1/ella/today-card/health", "get_today_card_health"),
        ("GET", "/v1/ella/today-cards/{card_id}", "get_today_card_by_id"),
        ("POST", "/v1/ella/today-cards/{card_id}/feedback", "submit_today_card_feedback"),
    ),
    _group(
        "today_cards",
        "today_card_service_exact_subject",
        "internal_only",
        ("POST", "/v1/ella/internal/today-cards/materialize", "materialize_today_card"),
        ("POST", "/v1/ella/internal/today-cards/invalidate-source", "invalidate_today_card_source"),
    ),
    _group(
        "voice",
        "firebase_exact_owner",
        "staged_public",
        ("GET", "/v1/voice/providers", "get_voice_providers"),
        ("GET", "/v1/entitlement", "get_voice_entitlement"),
        ("GET", "/v1/voice/entitlement", "get_voice_entitlement"),
        ("GET", "/v1/voice/health", "voice_health"),
    ),
    _group(
        "voice",
        "firebase_exact_owner_with_consent",
        "staged_public",
        ("POST", "/v1/voice/session", "create_voice_session"),
    ),
    _group(
        "voice",
        "voice_proxy_session_exact_subject",
        "internal_only",
        ("POST", "/v1/voice/canary/accept", "accept_voice_canary_session"),
        ("POST", "/v1/voice/canary/heartbeat", "heartbeat_voice_canary_session"),
        ("POST", "/v1/voice/canary/complete", "complete_voice_canary_session"),
        ("POST", "/v1/voice/context", "get_voice_context"),
        ("POST", "/v1/voice/tool", "execute_voice_tool"),
        ("POST", "/v1/voice/search-omi", "search_omi_conversations"),
        ("POST", "/v1/voice/search", "unified_search"),
    ),
    _group(
        "voice",
        "public_metadata",
        "public",
        ("GET", "/v1/voice/config", "get_voice_config"),
    ),
    _group(
        "voice",
        "firebase_or_internal_tts_exact_subject",
        "staged_public",
        ("POST", "/v1/voice/tts", "synthesize_speech"),
    ),
)


def _declared_routes():
    declared = {}
    for module, authority, disposition, routes in ROUTE_GROUPS:
        for method, path, endpoint in routes:
            key = (module, method, path)
            assert key not in declared, f"duplicate route declaration: {key}"
            declared[key] = (endpoint, authority, disposition)
    return declared


def _source_routes(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prefixes = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Assign, ast.AnnAssign)):
            continue
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        value = node.value
        if not isinstance(value, ast.Call):
            continue
        function_name = value.func.id if isinstance(value.func, ast.Name) else getattr(value.func, "attr", "")
        if function_name != "APIRouter":
            continue
        prefix = ""
        for keyword in value.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                prefix = keyword.value.value
        for target in targets:
            if isinstance(target, ast.Name):
                prefixes[target.id] = prefix

    routes = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            if not (
                isinstance(decorator, ast.Call)
                and isinstance(decorator.func, ast.Attribute)
                and decorator.func.attr.upper() in HTTP_METHODS
                and decorator.args
                and isinstance(decorator.args[0], ast.Constant)
                and isinstance(decorator.func.value, ast.Name)
            ):
                continue
            method = decorator.func.attr.upper()
            route_path = prefixes.get(decorator.func.value.id, "") + decorator.args[0].value
            key = (path.stem, method, route_path)
            assert key not in routes, f"duplicate source route: {key}"
            routes[key] = node.name
    return routes


def test_all_ella_router_modules_and_routes_have_declared_authority_and_disposition():
    module_paths = {path.stem: path for path in ROUTERS.glob("*.py") if path.name != "__init__.py"}
    declared_modules = {module for module, *_rest in ROUTE_GROUPS} | {"auto_provision"}
    assert set(module_paths) == declared_modules

    actual = {}
    for path in module_paths.values():
        actual.update(_source_routes(path))

    declared = _declared_routes()
    assert set(actual) == set(declared)
    for key, endpoint_name in actual.items():
        declared_endpoint, authority, disposition = declared[key]
        assert endpoint_name == declared_endpoint
        assert authority
        assert disposition


def test_every_router_source_is_registered_without_disabling_conditional_routers():
    ella_init = (BACKEND / "ella" / "__init__.py").read_text(encoding="utf-8")
    main_source = (BACKEND / "main.py").read_text(encoding="utf-8")
    modules_with_routes = {module for module, *_rest in ROUTE_GROUPS}
    for module in modules_with_routes - {"ai_consent"}:
        assert f"from ella.routers.{module} import" in ella_init, module
    assert "app.include_router(ai_consent.router)" in main_source
    assert 'if ELLA_GUARDIAN_ENABLED:' in ella_init
    assert 'if ELLA_VOICE_V2_ENABLED:' in ella_init
