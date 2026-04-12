from ella.services.escalation_policy import (
    CHANNEL_EMAIL,
    CHANNEL_GUARDIAN_AUDIO,
    CHANNEL_IMESSAGE,
    DECISION_ASK_USER_FIRST,
    DECISION_LOG_ONLY,
    DECISION_NOTIFY_NOW,
    DECISION_QUEUE_FOR_REPORT,
    CaregiverPolicyContext,
    EscalationEvent,
    UserPolicyContext,
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
        "email": "caregiver@example.test",
        "phone": "+15550000002",
        "permissions": {},
    }
    data.update(overrides)
    return CaregiverPolicyContext(**data)


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


def test_critical_event_respects_disabled_caregiver_emergency_alert_permission():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode="alert"),
        [_caregiver(permissions={"receive_emergency_alerts": False})],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert len(decision.delivery_plan) == 1
    assert decision.delivery_plan[0].target == "user"


def test_critical_event_uses_email_when_caregiver_has_no_phone():
    decision = evaluate_escalation_policy(
        _event(severity="critical"),
        _user(guardian_mode=None),
        [_caregiver(phone=None)],
    )

    assert decision.decision == DECISION_NOTIFY_NOW
    assert len(decision.delivery_plan) == 1
    assert decision.delivery_plan[0].target == "emergency_caregiver"
    assert decision.delivery_plan[0].channel == CHANNEL_EMAIL


def test_high_event_asks_user_first_without_contacting_caregiver():
    decision = evaluate_escalation_policy(
        _event(severity="high"),
        _user(guardian_mode="cyborg"),
        [_caregiver()],
    )

    assert decision.decision == DECISION_ASK_USER_FIRST
    assert decision.requires_ack is True
    assert len(decision.delivery_plan) == 1
    assert decision.delivery_plan[0].target == "user"


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
