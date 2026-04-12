"""Deterministic caregiver escalation policy for Ella.

The scanner/main agent/Observer should classify events; this module decides
whether and how the system is allowed to notify users and caregivers.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

Severity = str
Decision = str

SEVERITY_CRITICAL = "critical"
SEVERITY_HIGH = "high"
SEVERITY_MEDIUM = "medium"
SEVERITY_LOW = "low"

DECISION_NOTIFY_NOW = "notify_now"
DECISION_ASK_USER_FIRST = "ask_user_first"
DECISION_QUEUE_FOR_REPORT = "queue_for_report"
DECISION_LOG_ONLY = "log_only"
DECISION_SUPPRESS = "suppress"

CHANNEL_GUARDIAN_AUDIO = "guardian_audio"
CHANNEL_IMESSAGE = "imessage"
CHANNEL_EMAIL = "email"


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
class DeliveryStep:
    target: str
    channel: str
    priority: str
    caregiver_id: Optional[str] = None
    fallback: Optional[str] = None
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {
            "target": self.target,
            "channel": self.channel,
            "priority": self.priority,
            "reason": self.reason,
        }
        if self.caregiver_id:
            data["caregiver_id"] = self.caregiver_id
        if self.fallback:
            data["fallback"] = self.fallback
        return data


@dataclass(frozen=True)
class EscalationPolicyDecision:
    decision: Decision
    reason: str
    trace_id: str
    requires_ack: bool = False
    delivery_plan: tuple[DeliveryStep, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision": self.decision,
            "reason": self.reason,
            "trace_id": self.trace_id,
            "requires_ack": self.requires_ack,
            "delivery_plan": [step.to_dict() for step in self.delivery_plan],
        }


def _normalize_severity(value: str) -> Severity:
    normalized = (value or "").strip().lower()
    if normalized in {SEVERITY_CRITICAL, SEVERITY_HIGH, SEVERITY_MEDIUM, SEVERITY_LOW}:
        return normalized
    return SEVERITY_LOW


def _guardian_audio_enabled(guardian_mode: Optional[str]) -> bool:
    if guardian_mode is None:
        return False
    normalized = str(guardian_mode).strip().lower()
    return normalized not in {"", "off", "none", "disabled", "null"}


def _permission_enabled(permissions: dict[str, Any], keys: tuple[str, ...], default: bool = False) -> bool:
    for key in keys:
        if key in permissions:
            return bool(permissions.get(key))
    return default


def _emergency_caregivers(caregivers: list[CaregiverPolicyContext]) -> list[CaregiverPolicyContext]:
    return [
        caregiver for caregiver in caregivers if caregiver.status.upper() == "ACTIVE" and caregiver.is_emergency_contact
    ]


def _critical_delivery_plan(
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> tuple[DeliveryStep, ...]:
    steps: list[DeliveryStep] = []

    if _guardian_audio_enabled(user.guardian_mode):
        steps.append(
            DeliveryStep(
                target="user",
                channel=CHANNEL_GUARDIAN_AUDIO,
                priority="urgent",
                reason="guardian_mode_active",
            )
        )

    for caregiver in _emergency_caregivers(caregivers):
        urgent_allowed = _permission_enabled(
            caregiver.permissions,
            ("receive_emergency_alerts", "emergency_alerts", "urgent_alerts"),
            default=True,
        )
        if not urgent_allowed:
            continue
        if caregiver.phone:
            steps.append(
                DeliveryStep(
                    target="emergency_caregiver",
                    caregiver_id=caregiver.caregiver_id,
                    channel=CHANNEL_IMESSAGE,
                    fallback=CHANNEL_EMAIL if caregiver.email else None,
                    priority="urgent",
                    reason="selected_emergency_contact",
                )
            )
        elif caregiver.email:
            steps.append(
                DeliveryStep(
                    target="emergency_caregiver",
                    caregiver_id=caregiver.caregiver_id,
                    channel=CHANNEL_EMAIL,
                    priority="urgent",
                    reason="selected_emergency_contact_no_phone",
                )
            )

    return tuple(steps)


def evaluate_escalation_policy(
    event: EscalationEvent,
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> EscalationPolicyDecision:
    """Return a deterministic delivery decision for a classified event."""
    severity = _normalize_severity(event.severity)
    trace_id = event.trace_id or event.uid
    ambiguous = event.ambiguity >= 0.7 or event.confidence < 0.5

    if severity == SEVERITY_CRITICAL:
        plan = _critical_delivery_plan(user, caregivers)
        if plan:
            return EscalationPolicyDecision(
                decision=DECISION_NOTIFY_NOW,
                reason="critical_event",
                trace_id=trace_id,
                requires_ack=True,
                delivery_plan=plan,
            )
        return EscalationPolicyDecision(
            decision=DECISION_LOG_ONLY,
            reason="critical_event_no_reachable_targets",
            trace_id=trace_id,
            requires_ack=True,
        )

    if ambiguous:
        return EscalationPolicyDecision(
            decision=DECISION_QUEUE_FOR_REPORT,
            reason="ambiguous_or_low_confidence",
            trace_id=trace_id,
        )

    if severity == SEVERITY_HIGH:
        plan: list[DeliveryStep] = []
        if _guardian_audio_enabled(user.guardian_mode):
            plan.append(
                DeliveryStep(
                    target="user",
                    channel=CHANNEL_GUARDIAN_AUDIO,
                    priority="high",
                    reason="ask_user_before_caregiver_escalation",
                )
            )
        elif user.phone:
            plan.append(
                DeliveryStep(
                    target="user",
                    channel=CHANNEL_IMESSAGE,
                    fallback=CHANNEL_EMAIL if user.user_email else None,
                    priority="high",
                    reason="guardian_audio_unavailable",
                )
            )
        return EscalationPolicyDecision(
            decision=DECISION_ASK_USER_FIRST if plan else DECISION_QUEUE_FOR_REPORT,
            reason="high_event_user_first",
            trace_id=trace_id,
            requires_ack=bool(plan),
            delivery_plan=tuple(plan),
        )

    if severity == SEVERITY_MEDIUM or event.event_type == "routine_report":
        return EscalationPolicyDecision(
            decision=DECISION_QUEUE_FOR_REPORT,
            reason="reportable_nonurgent_event",
            trace_id=trace_id,
        )

    return EscalationPolicyDecision(
        decision=DECISION_LOG_ONLY,
        reason="low_severity_event",
        trace_id=trace_id,
    )


def build_plain_language_policy_view(
    user: UserPolicyContext,
    caregivers: list[CaregiverPolicyContext],
) -> dict[str, Any]:
    """Return the read-only effective escalation policy for UI display."""
    emergency_caregivers = _emergency_caregivers(caregivers)
    emergency_caregiver = emergency_caregivers[0] if emergency_caregivers else None
    guardian_audio_enabled = _guardian_audio_enabled(user.guardian_mode)

    def channel_status(channel: str, enabled: bool, reason: str) -> dict[str, Any]:
        return {
            "channel": channel,
            "enabled": enabled,
            "reason": reason,
        }

    caregiver_views: list[dict[str, Any]] = []
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
                        bool(caregiver.phone),
                        "Phone number on file" if caregiver.phone else "No phone number on file",
                    ),
                    channel_status(
                        CHANNEL_EMAIL,
                        bool(caregiver.email),
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
                    if caregiver.is_emergency_contact and emergency_alerts
                    else "This caregiver will not receive immediate emergency alerts unless you enable it."
                ),
            }
        )

    emergency_contact_text = (
        f"{emergency_caregiver.name or 'Your emergency caregiver'} is selected as the emergency contact."
        if emergency_caregiver
        else "No active emergency caregiver is selected."
    )

    critical_targets = ["you"]
    if emergency_caregiver:
        critical_targets.append("your selected emergency caregiver")

    rules = [
        {
            "severity": SEVERITY_CRITICAL,
            "decision": DECISION_NOTIFY_NOW if emergency_caregiver or guardian_audio_enabled else DECISION_LOG_ONLY,
            "title": "Critical safety concerns",
            "text": (
                "Critical safety concerns notify "
                + " and ".join(critical_targets)
                + " immediately when those channels are available."
            ),
        },
        {
            "severity": SEVERITY_HIGH,
            "decision": DECISION_ASK_USER_FIRST,
            "title": "High concern events",
            "text": "High concern events try to ask you first before notifying a caregiver.",
        },
        {
            "severity": SEVERITY_MEDIUM,
            "decision": DECISION_QUEUE_FOR_REPORT,
            "title": "Medium or ambiguous events",
            "text": "Medium or ambiguous events are saved for review or a recap unless they repeat or get worse.",
        },
        {
            "severity": SEVERITY_LOW,
            "decision": DECISION_LOG_ONLY,
            "title": "Low concern events",
            "text": "Low concern events are logged only and do not notify caregivers immediately.",
        },
    ]

    display_rules = [rule["text"] for rule in rules]
    display_rules.append(
        "Caregiver reports are privacy-filtered and should include trends or concerns, not raw private chats."
    )

    return {
        "policy_version": "ella.escalation_policy.v1",
        "source": "omi_backend",
        "uid": user.uid,
        "user": {
            "guardian_mode": user.guardian_mode,
            "channels": [
                channel_status(
                    CHANNEL_GUARDIAN_AUDIO,
                    guardian_audio_enabled,
                    "Guardian audio mode is active" if guardian_audio_enabled else "Guardian audio mode is off",
                ),
                channel_status(
                    CHANNEL_IMESSAGE,
                    bool(user.user_phone),
                    "Phone number on file" if user.user_phone else "No phone number on file",
                ),
                channel_status(
                    CHANNEL_EMAIL,
                    bool(user.user_email),
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
            "Emergency alerts can contact the selected emergency caregiver when policy allows it.",
            "Caregiver reports should include trends, concerns, and escalations, not raw private chats.",
            "The backend owns these rules so app displays do not drift from delivery behavior.",
        ],
        "display": {
            "title": "How Ella handles alerts",
            "subtitle": "These rules are resolved by the server from your current caregiver and channel settings.",
            "emergency_contact": emergency_contact_text,
            "rules": display_rules,
        },
    }
