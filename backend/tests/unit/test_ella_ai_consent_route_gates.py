import ast
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[2]


def _gated_route_paths(source_path: Path, dependency_name: str) -> set[str]:
    tree = ast.parse(source_path.read_text())
    gated_paths = set()

    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        signature_uses_dependency = dependency_name in ast.unparse(node.args)
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call) or not decorator.args:
                continue
            path_arg = decorator.args[0]
            if not isinstance(path_arg, ast.Constant) or not isinstance(path_arg.value, str):
                continue
            dependencies = next(
                (keyword.value for keyword in decorator.keywords if keyword.arg == "dependencies"),
                None,
            )
            decorator_uses_dependency = dependencies is not None and dependency_name in ast.unparse(dependencies)
            if signature_uses_dependency or decorator_uses_dependency:
                gated_paths.add(path_arg.value)

    return gated_paths


def _function_source(source_path: Path, function_name: str) -> str:
    source = source_path.read_text()
    tree = ast.parse(source)
    function = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == function_name
    )
    return ast.get_source_segment(source, function)


def test_authenticated_ai_egress_routes_share_the_consent_gate():
    assert "/chat/stream" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "chat.py",
        "require_current_ai_consent",
    )
    assert "/ensure" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "onboarding.py",
        "require_current_ai_consent",
    )
    assert "/session" in _gated_route_paths(
        BACKEND / "ella" / "routers" / "voice.py",
        "require_current_ai_consent",
    )
    assert "/v4/listen" in _gated_route_paths(
        BACKEND / "routers" / "transcribe.py",
        "require_current_ai_consent",
    )


def test_first_message_websocket_auth_checks_consent_before_streaming():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    auth_position = source.index("uid = auth.get_current_user_uid_from_ws_message(first_message)")
    consent_position = source.index("assert_current_ai_consent(uid)", auth_position)
    stream_position = source.index("await _stream_handler(", consent_position)

    assert auth_position < consent_position < stream_position
    assert '"error": detail.get("code", "ai_consent_required")' in source


def test_tts_route_uses_authenticated_or_internal_service_gate():
    voice_source_path = BACKEND / "ella" / "routers" / "voice.py"
    assert "/tts" in _gated_route_paths(
        voice_source_path,
        "require_current_ai_consent_or_internal_tts",
    )
    voice_source = voice_source_path.read_text()
    assert "if resolve_processor(provider) is None:" in voice_source
    guardian_source = (BACKEND / "ella" / "routers" / "guardian.py").read_text()
    assert 'headers["X-Ella-Subject-Uid"] = req.uid' in guardian_source


def test_selected_stt_provider_must_map_to_disclosed_recipient():
    source = (BACKEND / "routers" / "transcribe.py").read_text()

    selection_position = source.index("selected_stt_service = stt_service")
    disclosure_position = source.index(
        'if resolve_processor(_stt_service_value(stt_service) or "") is None:',
        selection_position,
    )
    provider_connect_position = source.index("# DEEPGRAM", disclosure_position)

    assert selection_position < disclosure_position < provider_connect_position


def test_legacy_payload_routes_cannot_bypass_consent_gate():
    protected_paths = {
        "/v2/messages",
        "/v2/initial-message",
        "/v2/voice-messages",
        "/v2/voice-message/transcribe",
        "/v2/files",
        "/v1/files",
        "/v1/initial-message",
    }
    gated_paths = _gated_route_paths(
        BACKEND / "routers" / "chat.py",
        "require_current_ai_consent",
    )

    assert protected_paths <= gated_paths


def test_stored_transcript_processing_routes_require_current_consent():
    protected_paths = {
        "/v1/conversations",
        "/v1/conversations/{conversation_id}/reprocess",
        "/v1/conversations/{conversation_id}/test-prompt",
        "/v1/conversations/merge",
    }
    gated_paths = _gated_route_paths(
        BACKEND / "routers" / "conversations.py",
        "require_current_ai_consent",
    )

    assert protected_paths <= gated_paths


def test_shared_conversation_processor_gates_target_uid_before_model_work():
    source_path = BACKEND / "utils" / "conversations" / "process_conversation.py"
    source = _function_source(source_path, "process_conversation_with_outcome")

    assert source.index("assert_current_ai_consent(uid)") < source.index("_get_structured(")
    for caller in ("integration.py", "workflow.py", "developer.py"):
        caller_source = (BACKEND / "routers" / caller).read_text()
        assert "process_conversation(" in caller_source or "process_conversation_with_outcome(" in caller_source


def test_stored_sync_gates_target_uid_before_deepgram_and_processing():
    source_path = BACKEND / "routers" / "sync.py"
    process_segment_source = _function_source(source_path, "process_segment")

    assert process_segment_source.index("assert_current_ai_consent(uid)") < process_segment_source.index(
        "deepgram_prerecorded("
    )
    assert "/v1/sync-local-files" in _gated_route_paths(source_path, "require_current_ai_consent")


def test_legacy_initial_message_helper_gates_every_model_call_path():
    source = _function_source(BACKEND / "routers" / "chat.py", "initial_message_util")

    assert source.index("assert_current_ai_consent(uid)") < source.index("initial_chat_message(")


def test_guardian_model_and_legacy_callback_tts_gate_target_uid():
    consolidate_source = _function_source(BACKEND / "ella" / "routers" / "guardian.py", "_consolidate_queue")
    notification_source = _function_source(BACKEND / "ella" / "routers" / "callbacks.py", "ella_notification")

    assert consolidate_source.index("assert_current_ai_consent(uid)") < consolidate_source.index("httpx.AsyncClient(")
    assert notification_source.index("assert_current_ai_consent(request.uid)") < notification_source.index(
        "_generate_tts_audio("
    )


def test_consent_authority_router_is_registered_outside_ella_feature_switch():
    main_source = (BACKEND / "main.py").read_text()
    ella_init_source = (BACKEND / "ella" / "__init__.py").read_text()

    assert "app.include_router(ai_consent.router)" in main_source
    assert "ai_consent_router" not in ella_init_source


def test_correction_submit_is_gated_but_receipt_and_undo_remain_available():
    gated_paths = _gated_route_paths(
        BACKEND / "ella" / "routers" / "corrections.py",
        "require_current_ai_consent",
    )

    assert "/v1/ella/conversations/{conversation_id}/corrections" in gated_paths
    assert "/v1/conversations/{conversation_id}/corrections" in gated_paths
    assert "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}" not in gated_paths
    assert "/v1/ella/conversations/{conversation_id}/corrections/{correction_id}/undo" not in gated_paths
