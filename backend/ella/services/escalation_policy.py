"""Deterministic caregiver escalation policy for Ella.

The scanner/main agent/Observer should classify events; this module decides
whether and how the system is allowed to notify users and caregivers.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

Severity = str
Decision = str

POLICY_VERSION = "ella.escalation_policy.v2"
CHANNEL_PREFERENCE_VERSION = "ella.channel_preferences.v1"

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"
SEVERITY_CYBORG = "cyborg"
SEVERITY_DAILY_RECAP = "daily_recap"

DECISION_NOTIFY_NOW = "notify_now"
DECISION_ASK_USER_FIRST = "ask_user_first"
DECISION_QUEUE_FOR_REPORT = "queue_for_report"
DECISION_LOG_ONLY = "log_only"
DECISION_SUPPRESS = "suppress"

CHANNEL_GUARDIAN_AUDIO = "guardian_audio"
CHANNEL_IMESSAGE = "imessage"
CHANNEL_EMAIL = "email"
CHANNEL_TWILIO_SMS = "twilio_sms"
CHANNEL_TWILIO_VOICE = "twilio_voice"
CHANNEL_IOS_VOICE_CALL = "ios_voice_call"

REASON_SELECTED = "selected"
REASON_GUARDIAN_DISABLED = "guardian_disabled"
REASON_CHANNEL_DISABLED_BY_USER = "channel_disabled_by_user"
REASON_PROVIDER_UNHEALTHY = "provider_unhealthy"
REASON_NO_PHONE = "no_phone"
REASON_NO_EMAIL = "no_email"
REASON_QUIET_HOURS = "quiet_hours"
REASON_CAREGIVER_NOT_ACTIVE = "caregiver_not_active"
REASON_CAREGIVER_PERMISSION_DENIED = "caregiver_permission_denied"
REASON_MODE_SUPPRESSED = "mode_suppressed"
REASON_DUPLICATE_SUPPRESSED = "duplicate_suppressed"

MODE_OFF = "off"
MODE_ACTIVE_SUPPORT = "active_support"
MODE_CYBORG = "cyborg"
MODE_DEMO = "demo"
MODE_CHATBOT = "chatbot"
MODE_EMERGENCY_ONLY = "emergency_only"
MODE_MEMORY_SUPPORT = "memory_support"
MODE_MAXIMUM_AWARENESS = "maximum_awareness"

EVENT_CLASS_DIRECT_USER_REQUEST = "direct_user_request"
EVENT_CLASS_MEMORY_SUPPORT = "memory_support"
EVENT_CLASS_MEDICATION_SUPPORT = "medication_support"
EVENT_CLASS_GENTLE_GUIDANCE = "gentle_guidance"
EVENT_CLASS_CRITICAL_SAFETY = "critical_safety"
EVENT_CLASS_CYBORG_CONTEXT = "cyborg_context"
EVENT_CLASS_REPORTABLE_TREND = "reportable_trend"
EVENT_CLASS_AMBIGUOUS_OR_LOW_CONFIDENCE = "ambiguous_or_low_confidence"

USER_DIRECT_RESPONSE_TYPES = {
    "assistant_request",
    "direct_question",
    "question",
    "user_request",
    "wake_word",
}

MEMORY_SUPPORT_TYPES = {
    "memory",
    "memory_recall",
    "recall",
    "recall_assistance",
}

MEDICATION_SUPPORT_TYPES = {
    "medication",
    "medication_question",
    "medication_support",
}

GENTLE_GUIDANCE_TYPES = {
    "cognitive",
    "emotional",
    "health",
    "gentle_guidance",
    "guidance",
    "routine_guidance",
    "schedule",
}

CRITICAL_SAFETY_TYPES = {
    "abuse",
    "chest_pain",
    "emergency",
    "fall",
    "fall_detection",
    "fire",
    "intruder",
    "medical_emergency",
    "safety",
    "self_harm",
    "wandering",
}

USER_CHANNELS = (
    CHANNEL_GUARDIAN_AUDIO,
    CHANNEL_IMESSAGE,
    CHANNEL_EMAIL,
    CHANNEL_TWILIO_SMS,
    CHANNEL_TWILIO_VOICE,
    CHANNEL_IOS_VOICE_CALL,
)

PHONE_CHANNELS = {
    CHANNEL_IMESSAGE,
    CHANNEL_TWILIO_SMS,
    CHANNEL_TWILIO_VOICE,
    CHANNEL_IOS_VOICE_CALL,
}

DEFAULT_CHANNEL_PREFERENCES: dict[str, dict[str, Any]] = {
    CHANNEL_GUARDIAN_AUDIO: {
        "enabled": True,
        "severities": [SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_CYBORG],
        "order": 10,
    },
    CHANNEL_IMESSAGE: {
        "enabled": True,
        "severities": [SEVERITY_CRITICAL, SEVERITY_HIGH],
        "order": 20,
    },
    CHANNEL_EMAIL: {
        "enabled": True,
        "severities": [SEVERITY_CRITICAL, SEVERITY_DAILY_RECAP],
        "order": 30,
    },
    CHANNEL_TWILIO_SMS: {
        "enabled": False,
        "severities": [SEVERITY_CRITICAL],
        "order": 40,
    },
    CHANNEL_TWILIO_VOICE: {
        "enabled": False,
        "severities": [SEVERITY_CRITICAL],
        "order": 50,
    },
    CHANNEL_IOS_VOICE_CALL: {
        "enabled": False,
        "severities": [SEVERITY_CRITICAL],
        "order": 60,
    },
}

DEFAULT_CAREGIVER_ALERT_PREFERENCES: dict[str, Any] = {
    "enabled": True,
    "emergency_contact_only": True,
    "channels": [CHANNEL_IMESSAGE, CHANNEL_EMAIL],
    "allow_high": False,
}

DEFAULT_RECAP_PREFERENCES: dict[str, Any] = {
    "user_daily": {"enabled": True, "channels": [CHANNEL_EMAIL]},
    "caregiver_daily": {"enabled": False, "channels": [CHANNEL_EMAIL], "privacy_filtered": True},
}


@dataclass(frozen=True)
class EscalationEvent:
    uid: str
    trace_id: str
    source: str
    event_type: str
    severity: Severity
    confidence: float
    ambiguity: float
    summary: str
    requested_channels: tuple[str, ...] = ()
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserPolicyContext:
    uid: str
    user_id: Optional[str] = None
    guardian_mode: Optional[str] = None
    user_email: Optional[str] = None
    user_phone: Optional[str] = None
    guardian_audio_enabled: Optional[bool] = None
    channel_preferences: dict[str, Any] = field(default_factory=dict)
    caregiver_alert_preferences: dict[str, Any] = field(default_factory=dict)
    recap_preferences: dict[str, Any] = field(default_factory=dict)
    provider_health: dict[str, bool] = field(default_factory=dict)
    quiet_hours_active: bool = False


@dataclass(frozen=True)
class CaregiverPolicyContext:
    caregiver_id: str
    status: str
    is_emergency_contact: bool
    name: Optional[str] = None
    relationship: Optional[str] = None
    email: Optional[str] = None
    phone: Optional[str] = None
    permissions: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryRule:
    rule_id: str
    event_class: str
    decision: Decision
    target_policy: str
    caregiver_policy: str
    preferred_channels: tuple[str, ...]
    applies_in_modes: tuple[str, ...]
    description: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "event_class": self.event_class,
            "decision": self.decision,
            "target_policy": self.target_policy,
            "caregiver_policy": self.caregiver_policy,
            "preferred_channels": list(self.preferred_channels),
            "applies_in_modes": list(self.applies_in_modes),
            "description": self.description,
        }


ALL_ACTIVE_MODES = (
    MODE_ACTIVE_SUPPORT,
    MODE_CYBORG,
    MODE_DEMO,
    MODE_CHATBOT,
    MODE_EMERGENCY_ONLY,
    MODE_MEMORY_SUPPORT,
    MODE_MAXIMUM_AWARENESS,
)

USER_FIRST_MODES = (
    MODE_ACTIVE_SUPPORT,
    MODE_CYBORG,
    MODE_DEMO,
    MODE_CHATBOT,
    MODE_EMERGENCY_ONLY,
    MODE_MEMORY_SUPPORT,
    MODE_MAXIMUM_AWARENESS,
    MODE_OFF,
)

DEFAULT_DELIVERY_RULES: tuple[DeliveryRule, ...] = (
    DeliveryRule(
        rule_id="direct-user-request-user-first",
        event_class=EVENT_CLASS_DIRECT_USER_REQUEST,
        decision=DECISION_ASK_USER_FIRST,
        target_policy="user_first",
        caregiver_policy="never",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=USER_FIRST_MODES,
        description="Wake words and direct questions are answered to the user, not sent to caregivers.",
    ),
    DeliveryRule(
        rule_id="memory-support-user-first",
        event_class=EVENT_CLASS_MEMORY_SUPPORT,
        decision=DECISION_ASK_USER_FIRST,
        target_policy="user_first",
        caregiver_policy="never",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=USER_FIRST_MODES,
        description="Memory recall and orientation support are delivered to the user first.",
    ),
    DeliveryRule(
        rule_id="medication-support-user-first",
        event_class=EVENT_CLASS_MEDICATION_SUPPORT,
        decision=DECISION_ASK_USER_FIRST,
        target_policy="user_first",
        caregiver_policy="never",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=USER_FIRST_MODES,
        description="Medication questions are answered to the user first without immediate caregiver delivery.",
    ),
    DeliveryRule(
        rule_id="gentle-guidance-user-first",
        event_class=EVENT_CLASS_GENTLE_GUIDANCE,
        decision=DECISION_ASK_USER_FIRST,
        target_policy="user_first",
        caregiver_policy="never",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=USER_FIRST_MODES,
        description="Routine guidance and noncritical support are routed to the user first.",
    ),
    DeliveryRule(
        rule_id="critical-safety-notify-now",
        event_class=EVENT_CLASS_CRITICAL_SAFETY,
        decision=DECISION_NOTIFY_NOW,
        target_policy="user_and_active_emergency_caregiver",
        caregiver_policy="active_emergency_only",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=ALL_ACTIVE_MODES + (MODE_OFF,),
        description="Critical safety events notify the user and configured active emergency caregivers.",
    ),
    DeliveryRule(
        rule_id="cyborg-context-user-only",
        event_class=EVENT_CLASS_CYBORG_CONTEXT,
        decision=DECISION_ASK_USER_FIRST,
        target_policy="user_first",
        caregiver_policy="never",
        preferred_channels=(CHANNEL_GUARDIAN_AUDIO, CHANNEL_IMESSAGE, CHANNEL_EMAIL),
        applies_in_modes=(MODE_CYBORG, MODE_DEMO, MODE_MAXIMUM_AWARENESS),
        description="Useful context in intelligence modes is delivered to the user only.",
    ),
    DeliveryRule(
        rule_id="reportable-trend-report-only",
        event_class=EVENT_CLASS_REPORTABLE_TREND,
        decision=DECISION_QUEUE_FOR_REPORT,
        target_policy="report",
        caregiver_policy="report_only",
        preferred_channels=(CHANNEL_EMAIL,),
        applies_in_modes=ALL_ACTIVE_MODES,
        description="Nonurgent trends are saved for review or recap instead of immediate caregiver alerts.",
    ),
)


@dataclass(frozen=True)
class DeliveryStep:
    target: str
    channel: str
    priority: str
    caregiver_id: Optional[str] = None
    fallback: Optional[str] = None
    reason: str = ""
    reason_code: str = REASON_SELECTED

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target": self.target,
            "channel": self.channel,
            "priority": self.priority,
            "reason": self.reason,
            "reason_code": self.reason_code,
        }
        if self.caregiver_id:
            data["caregiver_id"] = self.caregiver_id
        if self.fallback:
            data["fallback"] = self.fallback
        return data


@dataclass(frozen=True)
class SuppressedChannel:
    target: str
    channel: str
    reason_code: str
    caregiver_id: Optional[str] = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target": self.target,
            "channel": self.channel,
            "reason_code": self.reason_code,
        }
        if self.caregiver_id:
            data["caregiver_id"] = self.caregiver_id
        if self.detail:
            data["detail"] = self.detail
        return data


@dataclass(frozen=True)
class EscalationPolicyDecision:
    decision: Decision
    reason: str
    trace_id: str
    requires_ack: bool = False
    delivery_plan: tuple[DeliveryStep, ...] = ()
    suppressed_channels: tuple[SuppressedChannel, ...] = ()
    policy_snapshot: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        selected_channels = [step.to_dict() for step in self.delivery_plan]
        return {
            "decision": self.decision,
            "reason": self.reason,
            "trace_id": self.trace_id,
            "requires_ack": self.requires_ack,
            "delivery_plan": selected_channels,
            "selected_channels": selected_channels,
            "suppressed_channels": [suppressed.to_dict() for suppressed in self.suppressed_channels],
            "policy_snapshot": self.policy_snapshot,
            "mode": self.policy_snapshot.get("mode"),
            "channel_preference_version": self.policy_snapshot.get("channel_preference_version"),
        }


def _normalize_severity(value: str) -> Severity:
    normalized = (value or "").strip().lower()
    if normalized in {SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW}:
        return normalized
    return SEVERITY_LOW


def _normalize_mode(guardian_mode: Optional[str]) -> str:
    normalized = str(guardian_mode or "").strip().lower()
    if normalized in {"", "off", "none", "disabled", "null", "guardian_off"}:
        return MODE_OFF
    if normalized in {"active", "active_support", "support"}:
        return MODE_ACTIVE_SUPPORT
    if normalized in {"emergency", "emergency_only", "alert", "alerts_only"}:
        return MODE_EMERGENCY_ONLY
    if normalized in {"memory", "memory_support"}:
        return MODE_MEMORY_SUPPORT
    if normalized in {"maximum", "maximum_awareness", "max_awareness", "max"}:
        return MODE_MAXIMUM_AWARENESS
    if normalized in {"cyborg"}:
        return MODE_CYBORG
    if normalized in {"demo"}:
        return MODE_DEMO
    if normalized in {"chatbot", "chat"}:
        return MODE_CHATBOT
    return normalized


def _guardian_audio_enabled(user: UserPolicyContext | Optional[str]) -> bool:
    if isinstance(user, UserPolicyContext):
        if user.guardian_audio_enabled is not None:
            return bool(user.guardian_audio_enabled)
        return _normalize_mode(user.guardian_mode) != MODE_OFF
    return _normalize_mode(user) != MODE_OFF


def _permission_enabled(permissions: dict[str, Any], keys: tuple[str, ...], default: bool = False) -> bool:
    for key in keys:
        if key in permissions:
            return bool(permissions.get(key))
    return default


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _list_value(value: Any, default: list[str]) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    return list(default)


def _merge_channel_preferences(raw_preferences: dict[str, Any]) -> dict[str, dict[str, Any]]:
    raw_preferences = _as_dict(raw_preferences)
    merged: dict[str, dict[str, Any]] = {}
    for channel, default in DEFAULT_CHANNEL_PREFERENCES.items():
        override = _as_dict(raw_preferences.get(channel))
        merged[channel] = {
            "enabled": bool(override.get("enabled", default["enabled"])),
            "severities": _list_value(override.get("severities"), default["severities"]),
            "order": int(override.get("order", default["order"])),
        }
    return merged


def _merge_caregiver_alert_preferences(raw_preferences: dict[str, Any]) -> dict[str, Any]:
    raw_preferences = _as_dict(raw_preferences)
    return {
        "enabled": bool(raw_preferences.get("enabled", DEFAULT_CAREGIVER_ALERT_PREFERENCES["enabled"])),
        "emergency_contact_only": bool(
            raw_preferences.get(
                "emergency_contact_only",
                DEFAULT_CAREGIVER_ALERT_PREFERENCES["emergency_contact_only"],
            )
        ),
        "channels": _list_value(
            raw_preferences.get("channels"),
            DEFAULT_CAREGIVER_ALERT_PREFERENCES["channels"],
        ),
        "allow_high": bool(raw_preferences.get("allow_high", DEFAULT_CAREGIVER_ALERT_PREFERENCES["allow_high"])),
    }


def _merge_recap_preferences(raw_preferences: dict[str, Any]) -> dict[str, Any]:
    raw_preferences = _as_dict(raw_preferences)
    merged = {
        "user_daily": dict(DEFAULT_RECAP_PREFERENCES["user_daily"]),
        "caregiver_daily": dict(DEFAULT_RECAP_PREFERENCES["caregiver_daily"]),
    }
    for key in merged:
        override = _as_dict(raw_preferences.get(key))
        merged[key].update(override)
        merged[key]["enabled"] = bool(merged[key].get("enabled"))
        merged[key]["channels"] = _list_value(merged[key].get("channels"), DEFAULT_RECAP_PREFERENCES[key]["channels"])
    return merged


def _provider_healthy(user: UserPolicyContext, channel: str) -> bool:
    provider_health = user.provider_health or {}
    if channel in provider_health:
        return bool(provider_health[channel])
    return True


def _severity_allowed(channel_pref: dict[str, Any], severity: str, event_type: str) -> bool:
    severities = set(channel_pref.get("severities") or [])
    delivery_class = SEVERITY_DAILY_RECAP if event_type in {"daily_recap", "routine_report"} else severity
    return delivery_class in severities


def _target_has_contact(user: UserPolicyContext, channel: str) -> tuple[bool, str]:
    if channel in PHONE_CHANNELS:
        return bool(user.user_phone), REASON_NO_PHONE
    if channel == CHANNEL_EMAIL:
        return bool(user.user_email), REASON_NO_EMAIL
    return True, ""


def _caregiver_has_contact(caregiver: CaregiverPolicyContext, channel: str) -> tuple[bool, str]:
    if channel in PHONE_CHANNELS:
        return bool(caregiver.phone), REASON_NO_PHONE
    if channel == CHANNEL_EMAIL:
        return bool(caregiver.email), REASON_NO_EMAIL
    return True, ""


def _fallback_channel(primary: str, email_enabled: bool, contact_email: Optional[str]) -> Optional[str]:
    if primary != CHANNEL_EMAIL and email_enabled and contact_email:
        return CHANNEL_EMAIL
    return None


def _email_fallback_enabled(snapshot: dict[str, Any], severity: str, event_type: str) -> bool:
    email_pref = snapshot["channel_preferences"][CHANNEL_EMAIL]
    return bool(email_pref["enabled"]) and _severity_allowed(email_pref, severity, event_type)


def _email_fallback_enabled_for_user_response(snapshot: dict[str, Any]) -> bool:
    email_pref = snapshot["channel_preferences"][CHANNEL_EMAIL]
    return bool(email_pref["enabled"])


def _delivery_rules_for_mode(mode: str) -> list[dict[str, Any]]:
    return [rule.to_dict() for rule in DEFAULT_DELIVERY_RULES if mode in rule.applies_in_modes]


def _delivery_rule_for_event_class(event_class: str, mode: str) -> Optional[DeliveryRule]:
    for rule in DEFAULT_DELIVERY_RULES:
        if rule.event_class == event_class and mode in rule.applies_in_modes:
            return rule
    return None


def _policy_snapshot(user: UserPolicyContext, caregivers: list[CaregiverPolicyContext]) -> dict[str, Any]:
    mode = _normalize_mode(user.guardian_mode)
    channel_preferences = _merge_channel_preferences(user.channel_preferences)
    caregiver_alerts = _merge_caregiver_alert_preferences(user.caregiver_alert_preferences)
    recap_preferences = _merge_recap_preferences(user.recap_preferences)
    return {
        "policy_version": POLICY_VERSION,
        "channel_preference_version": CHANNEL_PREFERENCE_VERSION,
        "mode": mode,
        "raw_guardian_mode": user.guardian_mode,
        "guardian_audio_enabled": _guardian_audio_enabled(user),
        "channel_preferences": channel_preferences,
        "caregiver_alert_preferences": caregiver_alerts,
        "recap_preferences": recap_preferences,
        "provider_health": dict(user.provider_health or {}),
        "quiet_hours_active": bool(user.quiet_hours_active),
        "delivery_rules": _delivery_rules_for_mode(mode),
        "caregivers": [
            {
                "caregiver_id": caregiver.caregiver_id,
                "status": caregiver.status,
                "is_emergency_contact": caregiver.is_emergency_contact,
                "permissions": dict(caregiver.permissions or {}),
                "has_phone": bool(caregiver.phone),
                "has_email": bool(caregiver.email),
            }
            for caregiver in caregivers
        ],
    }


def _suppress(
    suppressed: list[SuppressedChannel],
    target: str,
    channel: str,
    reason_code: str,
    caregiver_id: Optional[str] = None,
    detail: str = "",
) -> None:
    suppressed.append(
        SuppressedChannel(
            target=target,
            channel=channel,
            caregiver_id=caregiver_id,
            reason_code=reason_code,
            detail=detail,
        )
    )


def _consider_user_channel(
    event: EscalationEvent,
    user: UserPolicyContext,
    snapshot: dict[str, Any],
    channel: str,
    priority: str,
    reason: str,
    selected: list[DeliveryStep],
    suppressed: list[SuppressedChannel],
) -> bool:
    severity = _normalize_severity(event.severity)
    channel_pref = snapshot["channel_preferences"][channel]
    mode = snapshot["mode"]
    if channel == CHANNEL_GUARDIAN_AUDIO and (mode == MODE_OFF or not snapshot["guardian_audio_enabled"]):
        _suppress(suppressed, "user", channel, REASON_GUARDIAN_DISABLED)
        return False
    if not channel_pref["enabled"] or not _severity_allowed(channel_pref, severity, event.event_type):
        _suppress(suppressed, "user", channel, REASON_CHANNEL_DISABLED_BY_USER)
        return False
    if not _provider_healthy(user, channel):
        _suppress(suppressed, "user", channel, REASON_PROVIDER_UNHEALTHY)
        return False
    if user.quiet_hours_active and severity != SEVERITY_CRITICAL:
        _suppress(suppressed, "user", channel, REASON_QUIET_HOURS)
        return False
    has_contact, reason_code = _target_has_contact(user, channel)
    if not has_contact:
        _suppress(suppressed, "user", channel, reason_code)
        return False
    selected.append(
        DeliveryStep(
            target="user",
            channel=channel,
            priority=priority,
            fallback=_fallback_channel(
                channel,
                _email_fallback_enabled(snapshot, severity, event.event_type),
                user.user_email,
            ),
            reason=reason,
        )
    )
    return True


def _consider_user_response_channel(
    user: UserPolicyContext,
    snapshot: dict[str, Any],
    channel: str,
    priority: str,
    reason: str,
    selected: list[DeliveryStep],
    suppressed: list[SuppressedChannel],
) -> bool:
    channel_pref = snapshot["channel_preferences"][channel]
    mode = snapshot["mode"]
    if channel == CHANNEL_GUARDIAN_AUDIO and (mode == MODE_OFF or not snapshot["guardian_audio_enabled"]):
        _suppress(suppressed, "user", channel, REASON_GUARDIAN_DISABLED)
        return False
    if not channel_pref["enabled"]:
        _suppress(suppressed, "user", channel, REASON_CHANNEL_DISABLED_BY_USER)
        return False
    if not _provider_healthy(user, channel):
        _suppress(suppressed, "user", channel, REASON_PROVIDER_UNHEALTHY)
        return False
    has_contact, reason_code = _target_has_contact(user, channel)
    if not has_contact:
        _suppress(suppressed, "user", channel, reason_code)
        return False
    selected.append(
        DeliveryStep(
            target="user",
            channel=channel,
            priority=priority,
            fallback=_fallback_channel(
                channel,
                _email_fallback_enabled_for_user_response(snapshot),
                user.user_email,
            ),
            reason=reason,
        )
    )
    return True


def _caregiver_alert_allowed(
    caregiver: CaregiverPolicyContext,
    caregiver_alerts: dict[str, Any],
    severity: str,
) -> tuple[bool, str]:
    if caregiver.status.upper() != "ACTIVE":
        return False, REASON_CAREGIVER_NOT_ACTIVE
    if caregiver_alerts["emergency_contact_only"] and not caregiver.is_emergency_contact:
        return False, REASON_CAREGIVER_PERMISSION_DENIED
    if severity == SEVERITY_HIGH and not caregiver_alerts.get("allow_high"):
        return False, REASON_CAREGIVER_PERMISSION_DENIED
    permission_keys = (
        ("receive_emergency_alerts", "emergency_alerts", "urgent_alerts")
        if severity == SEVERITY_CRITICAL
        else ("receive_high_alerts", "high_alerts")
    )
    default_allowed = severity == SEVERITY_CRITICAL
    if not _permission_enabled(caregiver.permissions, permission_keys, default=default_allowed):
        return False, REASON_CAREGIVER_PERMISSION_DENIED
    return True, ""


def _consider_caregiver_channels(
    caregiver: CaregiverPolicyContext,
    severity: str,
    user: UserPolicyContext,
    snapshot: dict[str, Any],
    priority: str,
    selected: list[DeliveryStep],
    suppressed: list[SuppressedChannel],
) -> None:
    caregiver_alerts = snapshot["caregiver_alert_preferences"]
    if not caregiver_alerts["enabled"]:
        for channel in caregiver_alerts["channels"]:
            _suppress(
                suppressed,
                "emergency_caregiver",
                channel,
                REASON_CAREGIVER_PERMISSION_DENIED,
                caregiver.caregiver_id,
                "caregiver alert delivery disabled",
            )
        return

    allowed, deny_reason = _caregiver_alert_allowed(caregiver, caregiver_alerts, severity)
    if not allowed:
        for channel in caregiver_alerts["channels"]:
            _suppress(suppressed, "emergency_caregiver", channel, deny_reason, caregiver.caregiver_id)
        return

    selected_caregiver_primary = False
    for channel in caregiver_alerts["channels"]:
        if channel == CHANNEL_EMAIL and selected_caregiver_primary:
            continue
        channel_pref = snapshot["channel_preferences"].get(channel)
        if not channel_pref or not channel_pref["enabled"] or severity not in set(channel_pref.get("severities") or []):
            _suppress(
                suppressed,
                "emergency_caregiver",
                channel,
                REASON_CHANNEL_DISABLED_BY_USER,
                caregiver.caregiver_id,
            )
            continue
        if not _provider_healthy(user, channel):
            _suppress(
                suppressed,
                "emergency_caregiver",
                channel,
                REASON_PROVIDER_UNHEALTHY,
                caregiver.caregiver_id,
            )
            continue
        has_contact, reason_code = _caregiver_has_contact(caregiver, channel)
        if not has_contact:
            _suppress(suppressed, "emergency_caregiver", channel, reason_code, caregiver.caregiver_id)
            continue
        selected.append(
            DeliveryStep(
                target="emergency_caregiver",
                caregiver_id=caregiver.caregiver_id,
                channel=channel,
                fallback=_fallback_channel(
                    channel,
                    CHANNEL_EMAIL in caregiver_alerts["channels"]
                    and _email_fallback_enabled(snapshot, severity, "safety"),
                    caregiver.email,
                ),
                priority=priority,
                reason="selected_emergency_contact" if severity == SEVERITY_CRITICAL else "selected_high_alert_contact",
            )
        )
        selected_caregiver_primary = True


def _is_cyborg_context_event(event: EscalationEvent) -> bool:
    return event.event_type in {"cyborg_context", "useful_context", "context"} or str(
        event.evidence.get("delivery_class", "")
    ).lower() == SEVERITY_CYBORG


def _normalized_event_labels(event: EscalationEvent) -> set[str]:
    labels = {event.event_type}
    for key in (
        "category",
        "delivery_class",
        "intent",
        "route",
        "scanner_category",
        "type",
        "urgency_type",
    ):
        value = event.evidence.get(key)
        if value is not None:
            labels.add(str(value))
    return {label.strip().lower() for label in labels if str(label).strip()}


def _is_critical_safety_event(event: EscalationEvent, severity: str) -> bool:
    if severity != SEVERITY_CRITICAL:
        return False
    if bool(event.evidence.get("emergency") or event.evidence.get("safety_critical")):
        return True
    return bool(_normalized_event_labels(event) & CRITICAL_SAFETY_TYPES)


def _classify_event(event: EscalationEvent, severity: str) -> str:
    labels = _normalized_event_labels(event)
    critical_safety = _is_critical_safety_event(event, severity)
    if labels & USER_DIRECT_RESPONSE_TYPES:
        return EVENT_CLASS_CRITICAL_SAFETY if critical_safety else EVENT_CLASS_DIRECT_USER_REQUEST
    if labels & MEMORY_SUPPORT_TYPES:
        return EVENT_CLASS_CRITICAL_SAFETY if critical_safety else EVENT_CLASS_MEMORY_SUPPORT
    if labels & MEDICATION_SUPPORT_TYPES:
        return EVENT_CLASS_CRITICAL_SAFETY if critical_safety else EVENT_CLASS_MEDICATION_SUPPORT
    if severity == SEVERITY_MEDIUM and labels & GENTLE_GUIDANCE_TYPES:
        return EVENT_CLASS_GENTLE_GUIDANCE
    if bool(event.evidence.get("user_response") or event.evidence.get("answer_user")):
        return EVENT_CLASS_CRITICAL_SAFETY if critical_safety else EVENT_CLASS_DIRECT_USER_REQUEST
    if critical_safety:
        return EVENT_CLASS_CRITICAL_SAFETY
    if _is_cyborg_context_event(event):
        return EVENT_CLASS_CYBORG_CONTEXT
    if severity in {SEVERITY_MEDIUM, SEVERITY_HIGH}:
        return EVENT_CLASS_REPORTABLE_TREND
    return EVENT_CLASS_AMBIGUOUS_OR_LOW_CONFIDENCE


def _is_user_first_response_event(event_class: str) -> bool:
    return event_class in {
        EVENT_CLASS_DIRECT_USER_REQUEST,
        EVENT_CLASS_MEMORY_SUPPORT,
        EVENT_CLASS_MEDICATION_SUPPORT,
        EVENT_CLASS_GENTLE_GUIDANCE,
    }


def _user_response_priority(severity: str) -> str:
    if severity == SEVERITY_CRITICAL:
        return "urgent"
    if severity == SEVERITY_HIGH:
        return "high"
    return "normal"


def _select_user_first_response(
    event: EscalationEvent,
    user: UserPolicyContext,
    snapshot: dict[str, Any],
    selected: list[DeliveryStep],
    suppressed: list[SuppressedChannel],
) -> None:
    priority = _user_response_priority(_normalize_severity(event.severity))
    _consider_user_response_channel(
        user,
        snapshot,
        CHANNEL_GUARDIAN_AUDIO,
        priority,
        "user_first_response_guardian_audio",
        selected,
        suppressed,
    )
    if not any(step.target == "user" for step in selected):
        for channel in (CHANNEL_IMESSAGE, CHANNEL_EMAIL):
            if _consider_user_response_channel(
                user,
                snapshot,
                channel,
                priority,
                "user_first_response_fallback",
                selected,
                suppressed,
            ):
                break


def evaluate_escalation_policy(
    event: EscalationEvent,
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> EscalationPolicyDecision:
    """Return a deterministic delivery decision for a classified event."""
    severity = _normalize_severity(event.severity)
    trace_id = event.trace_id or event.uid
    ambiguous = event.ambiguity >= 0.7 or event.confidence < 0.5
    snapshot = _policy_snapshot(user, caregivers)
    mode = snapshot["mode"]
    event_class = _classify_event(event, severity)
    selected: list[DeliveryStep] = []
    suppressed: list[SuppressedChannel] = []

    if bool(event.evidence.get("duplicate_suppressed") or event.evidence.get("duplicate")):
        for channel in USER_CHANNELS:
            _suppress(suppressed, "user", channel, REASON_DUPLICATE_SUPPRESSED)
        return EscalationPolicyDecision(
            decision=DECISION_SUPPRESS,
            reason=REASON_DUPLICATE_SUPPRESSED,
            trace_id=trace_id,
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if _is_user_first_response_event(event_class) and _delivery_rule_for_event_class(event_class, mode):
        _select_user_first_response(event, user, snapshot, selected, suppressed)
        return EscalationPolicyDecision(
            decision=DECISION_ASK_USER_FIRST if selected else DECISION_QUEUE_FOR_REPORT,
            reason="user_first_response" if selected else "user_first_response_no_reachable_user_channel",
            trace_id=trace_id,
            delivery_plan=tuple(selected),
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if severity in {SEVERITY_HIGH, SEVERITY_MEDIUM} and mode == MODE_EMERGENCY_ONLY:
        for channel in USER_CHANNELS:
            _suppress(suppressed, "user", channel, REASON_MODE_SUPPRESSED)
        return EscalationPolicyDecision(
            decision=DECISION_SUPPRESS,
            reason=REASON_MODE_SUPPRESSED,
            trace_id=trace_id,
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if severity == SEVERITY_CRITICAL:
        _consider_user_channel(
            event,
            user,
            snapshot,
            CHANNEL_GUARDIAN_AUDIO,
            "urgent",
            "critical_user_guardian_audio",
            selected,
            suppressed,
        )
        if not any(step.target == "user" for step in selected):
            for channel in (CHANNEL_IMESSAGE, CHANNEL_EMAIL):
                if _consider_user_channel(
                    event,
                    user,
                    snapshot,
                    channel,
                    "urgent",
                    "critical_user_fallback",
                    selected,
                    suppressed,
                ):
                    break
        for caregiver in caregivers:
            _consider_caregiver_channels(caregiver, severity, user, snapshot, "urgent", selected, suppressed)
        return EscalationPolicyDecision(
            decision=DECISION_NOTIFY_NOW if selected else DECISION_LOG_ONLY,
            reason="critical_event" if selected else "critical_event_no_reachable_targets",
            trace_id=trace_id,
            requires_ack=True,
            delivery_plan=tuple(selected),
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if ambiguous:
        return EscalationPolicyDecision(
            decision=DECISION_QUEUE_FOR_REPORT,
            reason="ambiguous_or_low_confidence",
            trace_id=trace_id,
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if severity == SEVERITY_HIGH:
        is_cyborg_context = (
            event_class == EVENT_CLASS_CYBORG_CONTEXT
            and _delivery_rule_for_event_class(EVENT_CLASS_CYBORG_CONTEXT, mode) is not None
        ) or mode == MODE_CYBORG
        _consider_user_channel(
            event,
            user,
            snapshot,
            CHANNEL_GUARDIAN_AUDIO,
            "high",
            "ask_user_before_caregiver_escalation",
            selected,
            suppressed,
        )
        if not selected:
            for channel in (CHANNEL_IMESSAGE, CHANNEL_EMAIL):
                if _consider_user_channel(
                    event,
                    user,
                    snapshot,
                    channel,
                    "high",
                    "guardian_audio_unavailable",
                    selected,
                    suppressed,
                ):
                    break
        if mode == MODE_MAXIMUM_AWARENESS and not is_cyborg_context:
            for caregiver in caregivers:
                _consider_caregiver_channels(caregiver, severity, user, snapshot, "high", selected, suppressed)
        return EscalationPolicyDecision(
            decision=DECISION_ASK_USER_FIRST if selected else DECISION_QUEUE_FOR_REPORT,
            reason="cyborg_context_user_only" if is_cyborg_context else "high_event_user_first",
            trace_id=trace_id,
            requires_ack=bool(selected),
            delivery_plan=tuple(selected),
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    if severity == SEVERITY_MEDIUM or event.event_type == "routine_report":
        if mode == MODE_OFF:
            _suppress(suppressed, "user", CHANNEL_GUARDIAN_AUDIO, REASON_GUARDIAN_DISABLED)
            return EscalationPolicyDecision(
                decision=DECISION_SUPPRESS,
                reason=REASON_GUARDIAN_DISABLED,
                trace_id=trace_id,
                suppressed_channels=tuple(suppressed),
                policy_snapshot=snapshot,
            )
        return EscalationPolicyDecision(
            decision=DECISION_QUEUE_FOR_REPORT,
            reason="reportable_nonurgent_event",
            trace_id=trace_id,
            suppressed_channels=tuple(suppressed),
            policy_snapshot=snapshot,
        )

    return EscalationPolicyDecision(
        decision=DECISION_LOG_ONLY,
        reason="low_severity_event",
        trace_id=trace_id,
        suppressed_channels=tuple(suppressed),
        policy_snapshot=snapshot,
    )


def _emergency_caregivers(caregivers: list[CaregiverPolicyContext]) -> list[CaregiverPolicyContext]:
    return [
        caregiver for caregiver in caregivers if caregiver.status.upper() == "ACTIVE" and caregiver.is_emergency_contact
    ]


def build_plain_language_policy_view(
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> dict[str, Any]:
    """Return the read-only effective escalation policy for UI display."""
    snapshot = _policy_snapshot(user, caregivers)
    emergency_caregivers = _emergency_caregivers(caregivers)
    emergency_caregiver = emergency_caregivers[0] if emergency_caregivers else None

    def channel_status(channel: str, enabled: bool, reason: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "enabled": enabled,
            "reason": reason,
        }

    caregiver_views: list[dict[str, Any]] = []
    caregiver_alerts = snapshot["caregiver_alert_preferences"]
    for caregiver in caregivers:
        permissions = caregiver.permissions or {}
        emergency_alerts = _permission_enabled(
            permissions,
            ("receive_emergency_alerts", "emergency_alerts", "urgent_alerts"),
            default=True,
        )
        daily_summary = _permission_enabled(
            permissions,
            ("receive_daily_summary", "daily_summary", "daily_summary_email"),
            default=False,
        )
        weekly_summary = _permission_enabled(
            permissions,
            ("receive_weekly_summary", "weekly_summary"),
            default=False,
        )
        caregiver_views.append(
            {
                "caregiver_id": caregiver.caregiver_id,
                "display_name": caregiver.name or "Caregiver",
                "relationship": caregiver.relationship,
                "status": caregiver.status,
                "is_emergency_contact": caregiver.is_emergency_contact,
                "channels": [
                    channel_status(
                        CHANNEL_IMESSAGE,
                        bool(caregiver.phone) and CHANNEL_IMESSAGE in caregiver_alerts["channels"],
                        "Phone number on file" if caregiver.phone else "No phone number on file",
                    ),
                    channel_status(
                        CHANNEL_EMAIL,
                        bool(caregiver.email) and CHANNEL_EMAIL in caregiver_alerts["channels"],
                        "Email on file" if caregiver.email else "No email on file",
                    ),
                ],
                "permissions": {
                    "emergency_alerts": emergency_alerts,
                    "daily_summary": daily_summary,
                    "weekly_summary": weekly_summary,
                },
                "plain_language": (
                    "This caregiver can receive urgent safety alerts."
                    if caregiver.status.upper() == "ACTIVE" and caregiver.is_emergency_contact and emergency_alerts
                    else "This caregiver will not receive immediate emergency alerts unless you enable it."
                ),
            }
        )

    emergency_contact_text = (
        f"{emergency_caregiver.name or 'Your emergency caregiver'} is selected as the emergency contact."
        if emergency_caregiver
        else "No active emergency caregiver is selected."
    )

    mode_text = {
        MODE_OFF: "Guardian audio is off. Critical events can still use configured non-audio fallback channels.",
        MODE_ACTIVE_SUPPORT: "Active Support answers user requests and routes safety/care events through user-first delivery before caregiver escalation.",
        MODE_CYBORG: "Cyborg mode sends useful context to the user first and does not notify caregivers unless an event is critical.",
        MODE_DEMO: "Demo mode uses user-first delivery for assistance and reserves caregiver escalation for critical safety demonstrations.",
        MODE_CHATBOT: "Chatbot mode is conversation-first and does not notify caregivers unless a critical safety event is produced.",
        MODE_EMERGENCY_ONLY: "Emergency Only suppresses non-critical delivery while preserving critical safety escalation.",
        MODE_MEMORY_SUPPORT: "Memory Support focuses on recall, orientation, medication, and routine guidance delivered to the user first.",
        MODE_MAXIMUM_AWARENESS: "Maximum Awareness uses user-first delivery and can include caregivers for high alerts when policy allows.",
    }.get(snapshot["mode"], "The configured mode is applied after channel consent and contact checks.")

    rules = [
        {
            "severity": SEVERITY_CRITICAL,
            "decision": DECISION_NOTIFY_NOW,
            "title": "Critical safety concerns",
            "text": "Critical safety concerns notify you and configured emergency caregivers through allowed channels.",
        },
        {
            "severity": SEVERITY_HIGH,
            "decision": DECISION_ASK_USER_FIRST,
            "title": "High concern events",
            "text": "High concern events ask you first; caregiver delivery is only added by explicit policy.",
        },
        {
            "severity": SEVERITY_MEDIUM,
            "decision": DECISION_QUEUE_FOR_REPORT,
            "title": "Medium or ambiguous events",
            "text": (
                "User-addressed questions and guidance are answered to the user first; other medium or ambiguous "
                "events are saved for review or a recap unless a stricter mode suppresses them."
            ),
        },
        {
            "severity": SEVERITY_LOW,
            "decision": DECISION_LOG_ONLY,
            "title": "Low concern events",
            "text": "Low concern events are logged only and do not notify caregivers immediately.",
        },
    ]

    display_rules = [mode_text] + [rule["text"] for rule in rules]
    display_rules.append(
        "Caregiver reports are privacy-filtered and should include trends or concerns, not raw private chats."
    )

    channel_preferences = snapshot["channel_preferences"]
    return {
        "policy_version": POLICY_VERSION,
        "source": "omi_backend",
        "uid": user.uid,
        "mode": snapshot["mode"],
        "channel_preference_version": CHANNEL_PREFERENCE_VERSION,
        "effective_policy": snapshot,
        "delivery_rules": snapshot["delivery_rules"],
        "user": {
            "guardian_mode": user.guardian_mode,
            "channels": [
                channel_status(
                    CHANNEL_GUARDIAN_AUDIO,
                    snapshot["guardian_audio_enabled"] and channel_preferences[CHANNEL_GUARDIAN_AUDIO]["enabled"],
                    "Guardian audio mode is active" if snapshot["guardian_audio_enabled"] else "Guardian audio mode is off",
                ),
                channel_status(
                    CHANNEL_IMESSAGE,
                    bool(user.user_phone) and channel_preferences[CHANNEL_IMESSAGE]["enabled"],
                    "Phone number on file" if user.user_phone else "No phone number on file",
                ),
                channel_status(
                    CHANNEL_EMAIL,
                    bool(user.user_email) and channel_preferences[CHANNEL_EMAIL]["enabled"],
                    "Email on file" if user.user_email else "No email on file",
                ),
            ],
        },
        "emergency_contact": {
            "configured": emergency_caregiver is not None,
            "caregiver_id": emergency_caregiver.caregiver_id if emergency_caregiver else None,
            "display_name": emergency_caregiver.name if emergency_caregiver else None,
            "status": emergency_caregiver.status if emergency_caregiver else None,
            "text": emergency_contact_text,
        },
        "caregivers": caregiver_views,
        "rules": rules,
        "privacy_notes": [
            "Emergency alerts can contact selected emergency caregivers when policy allows it.",
            "Caregiver reports should include trends, concerns, and escalations, not raw private chats.",
            "The backend owns these rules so app displays do not drift from delivery behavior.",
        ],
        "display": {
            "title": "How Ella handles alerts",
            "subtitle": "These rules are resolved by the server from your current caregiver and channel settings.",
            "mode": mode_text,
            "emergency_contact": emergency_contact_text,
            "rules": display_rules,
        },
    }
