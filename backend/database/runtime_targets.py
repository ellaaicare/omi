"""Shared lower-layer contract for exact Hermes Cloud runtime targets."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CLOUD_RUNTIME_PROVIDER = "hermes_cloud"
CLOUD_RUNTIME_MODEL = "gpt-5.6-terra"
CLOUD_RUNTIME_TARGET_MODES = (
    "hermes-cloud-chat",
    "hermes-cloud-voice",
    "hermes-cloud-transcript",
    "hermes-cloud-guardian",
)


@dataclass(frozen=True)
class RuntimeTargetLineage:
    """Consent lineage that must exactly match a published Cloud target."""

    policy_version: str
    processor_set_hash: str
    scope_version: str
    scope_hash: str

    def validate(self) -> "RuntimeTargetLineage":
        if not all(
            (
                self.policy_version,
                self.processor_set_hash,
                self.scope_version,
                self.scope_hash,
            )
        ):
            raise ValueError("cloud_runtime_lineage_incomplete")
        return self

    def as_dict(self) -> dict[str, str]:
        self.validate()
        return {
            "policy_version": self.policy_version,
            "processor_set_hash": self.processor_set_hash,
            "scope_version": self.scope_version,
            "scope_hash": self.scope_hash,
        }

    @classmethod
    def from_mapping(cls, value: Any) -> "RuntimeTargetLineage":
        mapping = value if isinstance(value, dict) else {}
        return cls(
            policy_version=str(mapping.get("policy_version") or ""),
            processor_set_hash=str(mapping.get("processor_set_hash") or ""),
            scope_version=str(mapping.get("scope_version") or ""),
            scope_hash=str(mapping.get("scope_hash") or ""),
        ).validate()
