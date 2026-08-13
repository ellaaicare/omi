"""Pure identity contract for durable Hermes Cloud conversation enrichment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

HERMES_CLOUD_ENRICHMENT_LEGACY_POLICY_VERSION = "hermes-cloud-enrichment-v1"
HERMES_CLOUD_ENRICHMENT_POLICY_VERSION = "hermes-cloud-enrichment-v2"
HERMES_CLOUD_ENRICHMENT_LEGACY_INSTRUCTIONS_SHA256 = "acb68ed9b263c62e8ed927c2e1461187cdff648bfb3a88e78ac401ed675d067d"

ENRICHMENT_INSTRUCTIONS = """You are Ella's OMI conversation enrichment worker.

Use the complete authoritative transcript in the input. Return exactly one JSON object and no surrounding prose.

Rules:
- Ground every claim in the transcript.
- Do not invent people, locations, relationships, health facts, or actions.
- The overview must start with "[Ella] ".
- Keep the title short and free of markdown.
- category must be one of: personal, education, health, finance, legal, philosophy, spiritual, science, entrepreneurship, parenting, romantic, travel, inspiration, technology, business, social, work, sports, politics, literature, history, architecture, music, weather, news, entertainment, psychology, real, design, family, economics, environment, other.
- Include concise ella_tags and an ella_signal object.
- Do not contact anyone, deliver Guardian or caregiver messages, or mutate memory.

Return exactly:
{
  "title": "short title",
  "overview": "[Ella] grounded enriched summary",
  "emoji": "one emoji",
  "category": "category",
  "ella_tags": ["omi", "enriched"],
  "ella_signal": {
    "salience": "low|medium|high",
    "memory_promotion": "none|candidate|promoted",
    "noise_level": "none|low|medium|high",
    "contains_media": false,
    "contains_user_speech": true,
    "guardian_relevant": false
  }
}"""


@dataclass(frozen=True)
class HermesCloudEnrichmentIdentity:
    job_id: str
    client_interaction_id: str
    trace_id: str
    transcript_sha256: str
    policy_version: str


def transcript_source(conversation: dict[str, Any]) -> tuple[str, str]:
    source = json.dumps(
        conversation.get("transcript_segments") or [],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return source, hashlib.sha256(source.encode("utf-8")).hexdigest()


def _interaction_identity(
    uid: str,
    conversation_id: str,
    transcript_sha256: str,
    active_summary_version_id: str,
) -> tuple[str, str]:
    return _interaction_identity_for_policy(
        uid,
        conversation_id,
        transcript_sha256,
        policy_version=HERMES_CLOUD_ENRICHMENT_POLICY_VERSION,
        active_summary_version_id=active_summary_version_id,
    )


def _interaction_identity_for_policy(
    uid: str,
    conversation_id: str,
    transcript_sha256: str,
    *,
    policy_version: str,
    active_summary_version_id: str | None,
) -> tuple[str, str]:
    policy_sha256 = (
        HERMES_CLOUD_ENRICHMENT_LEGACY_INSTRUCTIONS_SHA256
        if policy_version == HERMES_CLOUD_ENRICHMENT_LEGACY_POLICY_VERSION
        else hashlib.sha256(ENRICHMENT_INSTRUCTIONS.encode("utf-8")).hexdigest()
    )
    identity_parts = [uid, conversation_id, transcript_sha256]
    if active_summary_version_id is not None:
        identity_parts.append(active_summary_version_id)
    identity_parts.extend((policy_version, policy_sha256))
    digest = hashlib.sha256("|".join(identity_parts).encode("utf-8")).hexdigest()
    return f"omi-enrichment:{digest}", f"omi-enrichment:{digest[:32]}"


def _build_enrichment_identity(
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
    policy_version: str,
    active_summary_version_id: str | None,
) -> HermesCloudEnrichmentIdentity:
    _, transcript_sha256 = transcript_source(conversation)
    client_interaction_id, trace_id = _interaction_identity_for_policy(
        uid,
        conversation_id,
        transcript_sha256,
        policy_version=policy_version,
        active_summary_version_id=active_summary_version_id,
    )
    return HermesCloudEnrichmentIdentity(
        job_id=("hce_" + hashlib.sha256(client_interaction_id.encode("utf-8")).hexdigest()),
        client_interaction_id=client_interaction_id,
        trace_id=trace_id,
        transcript_sha256=transcript_sha256,
        policy_version=policy_version,
    )


def build_enrichment_identity(
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
) -> HermesCloudEnrichmentIdentity:
    active_summary_version_id = str(conversation.get("active_summary_version_id") or "unversioned")
    return _build_enrichment_identity(
        uid=uid,
        conversation_id=conversation_id,
        conversation=conversation,
        policy_version=HERMES_CLOUD_ENRICHMENT_POLICY_VERSION,
        active_summary_version_id=active_summary_version_id,
    )


def build_legacy_enrichment_identity(
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
) -> HermesCloudEnrichmentIdentity:
    """Reproduce the deployed v1 identity for already-durable outbox jobs."""
    return _build_enrichment_identity(
        uid=uid,
        conversation_id=conversation_id,
        conversation=conversation,
        policy_version=HERMES_CLOUD_ENRICHMENT_LEGACY_POLICY_VERSION,
        active_summary_version_id=None,
    )
