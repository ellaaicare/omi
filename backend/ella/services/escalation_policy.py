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
