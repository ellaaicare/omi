from ella.services.escalation_policy import (
    CHANNEL_EMAIL,
    CHANNEL_GUARDIAN_AUDIO,
    CHANNEL_IMESSAGE,
    DECISION_ASK_USER_FIRST,
    DECISION_LOG_ONLY,
    DECISION_NOTIFY_NOW,
    DECISION_QUEUE_FOR_REPORT,
    DECISION_SUPPRESS,
    REASON_CAREGIVER_NOT_ACTIVE,
    REASON_CAREGIVER_PERMISSION_DENIED,
    REASON_CHANNEL_DISABLED_BY_USER,
    REASON_GUARDIAN_DISABLED,
    REASON_NO_EMAIL,
    REASON_NO_PHONE,
    REASON_MODE_SUPPRESSED,
    CaregiverPolicyContext,
    EscalationEvent,
    UserPolicyContext,
    build_plain_language_policy_view,
    evaluate_escalation_policy,
)


def _event(**overrides):
    data = {
        "uid": "uid-1",
        "trace_id": "trace-1",
        "source": "scanner",
        "event_type": "safety",
        "severity": "low",
        "confidence": 0.9,
        "ambiguity": 0.0,
        "summary": "test",
    }
    data.update(overrides)
    return EscalationEvent(**data)


def _user(**overrides):
    data = {
        "uid": "uid-1",
        "user_id": "user-1",
        "guardian_mode": "cyborg",
        "user_email": "user@example.test",
        "user_phone": "+15550000001",
    }
    data.update(overrides)
    return UserPolicyContext(**data)


def _caregiver(**overrides):
    data = {
        "caregiver_id": "caregiver-1",
        "status": "ACTIVE",
        "is_emergency_contact": True,
        "name": "Emily",
        "relationship": "daughter",
        "email": "caregiver@example.test",
        "phone": "+15550000002",
        "permissions": {},
    }
    data.update(overrides)
    return CaregiverPolicyContext(**data)


def _reason_codes(decision):
    return {item.reason_code for item in decision.suppressed_channels}


def test_critical_event_notifies_user_audio_and_emergency_caregiver_imessage():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode="maximum_awareness"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert decision.requires_ack is True
    assert [step.channel for step in decision.delivery_plan] == [CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE]
    assert decision.delivery_plan[1].fallback == CHANNEL_EMAIL
    payload = decision.to_dict()
    assert payload["selected_channels"] == payload["delivery_plan"]
    assert payload["policy_snapshot"]["mode"] == "maximum_awareness"


def test_guardian_off_critical_suppresses_audio_but_allows_user_and_caregiver_fallbacks():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode="off"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert decision.delivery_plan[0].target == "user"
    assert decision.delivery_plan[0].channel == CHANNEL_IMESSAGE
    assert decision.delivery_plan[0].fallback == CHANNEL_EMAIL
    assert REASON_GUARDIAN_DISABLED in _reason_codes(decision)


def test_guardian_off_routine_is_suppressed_and_logged():
    decision = evaluate_escalation_policy(
        _event(severity="medium", event_type="routine_report"),
        _user(guardian_mode="off"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_SUPPRESS
    assert decision.delivery_plan == ()
    assert decision.reason == REASON_GUARDIAN_DISABLED


def test_cyborg_useful_context_is_user_only_guardian_audio():
    decision = evaluate_escalation_policy(
        _event(severity="high", event_type="useful_context"),
        _user(guardian_mode="cyborg"),
        [_caregiver(caregiver_id="caregiver-2")],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert decision.delivery_plan[0].channel == CHANNEL_GUARDIAN_AUDIO
    assert decision.reason == "cyborg_context_user_only"


def test_active_support_is_distinct_from_cyborg_mode():
    active_decision = evaluate_escalation_policy(
        _event(severity="high", event_type="useful_context"),
        _user(guardian_mode="ACTIVE_SUPPORT"),
        [_caregiver()],
    )
    cyborg_decision = evaluate_escalation_policy(
        _event(severity="high", event_type="useful_context"),
        _user(guardian_mode="CYBORG"),
        [_caregiver()],
    )

    assert active_decision.policy_snapshot["mode"] == "active_support"
    assert cyborg_decision.policy_snapshot["mode"] == "cyborg"
    assert active_decision.reason == "high_event_user_first"
    assert cyborg_decision.reason == "cyborg_context_user_only"


def test_cyborg_critical_uses_normal_critical_path():
    decision = evaluate_escalation_policy(
        _event(severity="critical", event_type="safety"),
        _user(guardian_mode="cyborg"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert [step.target for step in decision.delivery_plan] == ["user", "emergency_caregiver"]


def test_wake_word_response_is_user_only_even_when_classifier_marks_critical():
    decision = evaluate_escalation_policy(
        _event(
            severity="critical",
            event_type="wake_word",
            evidence={"category": "wake_word"},
        ),
        _user(guardian_mode="cyborg"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert decision.reason == "user_first_response"
    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert decision.delivery_plan[0].channel == CHANNEL_GUARDIAN_AUDIO


def test_medication_question_routes_to_user_without_caregiver_alert():
    decision = evaluate_escalation_policy(
        _event(
            severity="medium",
            event_type="medication",
            evidence={"category": "medication"},
            summary="What medications am I supposed to take today?",
        ),
        _user(guardian_mode="cyborg"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert decision.delivery_plan[0].channel == CHANNEL_GUARDIAN_AUDIO
    assert decision.delivery_plan[0].priority == "normal"


def test_memory_recall_and_gentle_guidance_route_to_user_first():
    for event_type in ("memory_recall", "gentle_guidance"):
        decision = evaluate_escalation_policy(
            _event(severity="medium", event_type=event_type),
            _user(guardian_mode="cyborg"),
            [_caregiver()],
        )

        assert decision.decision == DECISION_ASK_USER_FIRST
        assert [step.target for step in decision.delivery_plan] == ["user"]
        assert decision.delivery_plan[0].channel == CHANNEL_GUARDIAN_AUDIO


def test_user_first_response_falls_back_to_user_imessage_when_guardian_audio_off():
    decision = evaluate_escalation_policy(
        _event(severity="medium", event_type="direct_question"),
        _user(guardian_mode="off"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert decision.delivery_plan[0].channel == CHANNEL_IMESSAGE
    assert decision.delivery_plan[0].fallback == CHANNEL_EMAIL
    assert REASON_GUARDIAN_DISABLED in _reason_codes(decision)


def test_emergency_only_medium_has_no_delivery():
    decision = evaluate_escalation_policy(
        _event(severity="medium"),
        _user(guardian_mode="emergency_only"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_SUPPRESS
    assert decision.delivery_plan == ()
    assert decision.reason == REASON_MODE_SUPPRESSED
    assert REASON_MODE_SUPPRESSED in _reason_codes(decision)


def test_maximum_awareness_high_is_user_first_and_caregiver_only_when_allowed():
    decision = evaluate_escalation_policy(
        _event(severity="high"),
        _user(
            guardian_mode="maximum_awareness",
            caregiver_alert_preferences={"allow_high": True},
        ),
        [_caregiver(permissions={"receive_high_alerts": True})],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert [step.target for step in decision.delivery_plan] == ["user", "emergency_caregiver"]
    assert decision.delivery_plan[0].channel == CHANNEL_GUARDIAN_AUDIO
    assert decision.delivery_plan[1].channel == CHANNEL_IMESSAGE


def test_maximum_awareness_high_suppresses_caregiver_when_policy_denies_high_alerts():
    decision = evaluate_escalation_policy(
        _event(severity="high"),
        _user(guardian_mode="maximum_awareness"),
        [_caregiver()],
    )

    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert REASON_CAREGIVER_PERMISSION_DENIED in _reason_codes(decision)


def test_imessage_disabled_uses_email_fallback_path():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(
            guardian_mode="off",
            channel_preferences={CHANNEL_IMESSAGE: {"enabled": False}},
        ),
        [],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert [step.channel for step in decision.delivery_plan] == [CHANNEL_EMAIL]
    assert REASON_CHANNEL_DISABLED_BY_USER in _reason_codes(decision)


def test_email_disabled_removes_email_fallback():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(
            guardian_mode="off",
            channel_preferences={CHANNEL_EMAIL: {"enabled": False}},
        ),
        [_caregiver()],
    )

    user_step = next(step for step in decision.delivery_plan if step.target == "user")
    caregiver_step = next(step for step in decision.delivery_plan if step.target == "emergency_caregiver")
    assert user_step.channel == CHANNEL_IMESSAGE
    assert user_step.fallback is None
    assert caregiver_step.fallback is None


def test_caregiver_inactive_returns_suppressed_reason():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode="cyborg"),
        [_caregiver(status="REMOVED")],
    )

    assert [step.target for step in decision.delivery_plan] == ["user"]
    assert REASON_CAREGIVER_NOT_ACTIVE in _reason_codes(decision)


def test_critical_event_respects_disabled_caregiver_emergency_alert_permission():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode="emergency_only"),
        [_caregiver(permissions={"receive_emergency_alerts": False})],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert len(decision.delivery_plan) == 1
    assert decision.delivery_plan[0].target == "user"
    assert REASON_CAREGIVER_PERMISSION_DENIED in _reason_codes(decision)


def test_critical_event_uses_email_when_caregiver_has_no_phone():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode=None, user_phone=None, user_email=None),
        [_caregiver(phone=None)],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert len(decision.delivery_plan) == 1
    assert decision.delivery_plan[0].target == "emergency_caregiver"
    assert decision.delivery_plan[0].channel == CHANNEL_EMAIL
    assert {REASON_GUARDIAN_DISABLED, REASON_NO_PHONE, REASON_NO_EMAIL}.issubset(_reason_codes(decision))


def test_ambiguous_medium_event_queues_for_report():
    decision = evaluate_escalation_policy(
        _event(severity="medium", confidence=0.4, ambiguity=0.8),
        _user(),
        [_caregiver()],
    )

    assert decision.decision == DECISION_QUEUE_FOR_REPORT
    assert decision.delivery_plan == ()


def test_low_event_logs_only():
    decision = evaluate_escalation_policy(
        _event(severity="low"),
        _user(),
        [_caregiver()],
    )

    assert decision.decision == DECISION_LOG_ONLY
    assert decision.delivery_plan == ()


def test_plain_language_policy_view_summarizes_effective_channels_and_rules():
    policy = build_plain_language_policy_view(
        _user(guardian_mode="cyborg"),
        [_caregiver(permissions={"receive_emergency_alerts": True, "receive_daily_summary": True})],
    )

    assert policy["policy_version"] == "ella.escalation_policy.v2"
    assert policy["source"] == "omi_backend"
    assert policy["mode"] == "cyborg"
    assert policy["channel_preference_version"] == "ella.channel_preferences.v1"
    assert policy["delivery_rules"]
    assert policy["delivery_rules"][0]["event_class"] == "direct_user_request"
    assert policy["delivery_rules"][0]["caregiver_policy"] == "never"
    assert policy["effective_policy"]["caregiver_alert_preferences"]["emergency_contact_only"] is True
    assert policy["emergency_contact"] == {
        "configured": True,
        "caregiver_id": "caregiver-1",
        "display_name": "Emily",
        "status": "ACTIVE",
        "text": "Emily is selected as the emergency contact.",
    }
    assert policy["user"]["channels"][0] == {
        "channel": CHANNEL_GUARDIAN_AUDIO,
        "enabled": True,
        "reason": "Guardian audio mode is active",
    }
    assert policy["caregivers"][0]["display_name"] == "Emily"
    assert policy["caregivers"][0]["permissions"]["emergency_alerts"] is True
    assert policy["caregivers"][0]["permissions"]["daily_summary"] is True
    assert policy["rules"][0]["severity"] == "critical"
    assert "configured emergency caregivers" in policy["rules"][0]["text"]
    assert "not raw private chats" in policy["display"]["rules"][-1]


def test_plain_language_policy_view_handles_missing_emergency_contact():
    policy = build_plain_language_policy_view(
        _user(guardian_mode=None, user_email=None, user_phone=None),
        [_caregiver(is_emergency_contact=False, email=None, phone=None)],
    )

    assert policy["emergency_contact"]["configured"] is False
    assert policy["emergency_contact"]["text"] == "No active emergency caregiver is selected."
    assert policy["user"]["channels"] == [
        {
            "channel": CHANNEL_GUARDIAN_AUDIO,
            "enabled": False,
            "reason": "Guardian audio mode is off",
        },
        {
            "channel": CHANNEL_IMESSAGE,
            "enabled": False,
            "reason": "No phone number on file",
        },
        {
            "channel": CHANNEL_EMAIL,
            "enabled": False,
            "reason": "No email on file",
        },
    ]
