"""Pure identity contract for durable Hermes Cloud conversation enrichment."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

HERMES_CLOUD_ENRICHMENT_POLICY_VERSION = "hermes-cloud-enrichment-v1"

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
    policy_sha256 = hashlib.sha256(ENRICHMENT_INSTRUCTIONS.encode("utf-8")).hexdigest()
    digest = hashlib.sha256(
        (
            f"{uid}|{conversation_id}|{transcript_sha256}|{active_summary_version_id}|"
            f"{HERMES_CLOUD_ENRICHMENT_POLICY_VERSION}|{policy_sha256}"
        ).encode("utf-8")
    ).hexdigest()
    return f"omi-enrichment:{digest}", f"omi-enrichment:{digest[:32]}"


def build_enrichment_identity(
    *,
    uid: str,
    conversation_id: str,
    conversation: dict[str, Any],
) -> HermesCloudEnrichmentIdentity:
    _, transcript_sha256 = transcript_source(conversation)
    active_summary_version_id = str(conversation.get("active_summary_version_id") or "unversioned")
    client_interaction_id, trace_id = _interaction_identity(
        uid,
        conversation_id,
        transcript_sha256,
        active_summary_version_id,
    )
    return HermesCloudEnrichmentIdentity(
        job_id=("hce_" + hashlib.sha256(client_interaction_id.encode("utf-8")).hexdigest()),
        client_interaction_id=client_interaction_id,
        trace_id=trace_id,
        transcript_sha256=transcript_sha256,
    )
