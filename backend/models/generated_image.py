"""Public, provider-neutral references to privately stored generated images."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

GENERATED_IMAGE_ASSET_CONTRACT_VERSION = "ella.generated_image.asset.v1"
GENERATED_IMAGE_DELIVERY_PREFIX = "/v1/ella/generated-image-assets/"


class GeneratedImageAssetRef(BaseModel):
    """An attachable image reference safe to expose to authenticated clients.

    Provider URLs and storage keys deliberately do not cross this boundary. A
    reference is attachable only after moderation and canonical confirmation.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal["ella.generated_image.asset.v1"] = GENERATED_IMAGE_ASSET_CONTRACT_VERSION
    asset_id: str = Field(min_length=1, max_length=128)
    job_id: str = Field(min_length=1, max_length=128)
    receipt_id: str = Field(min_length=1, max_length=128)
    generation: int = Field(ge=1)
    media_type: Literal["image/jpeg", "image/png", "image/webp"]
    sha256: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    width: int = Field(ge=1, le=16384)
    height: int = Field(ge=1, le=16384)
    moderation_status: Literal["approved"] = "approved"
    alt_text: str = Field(min_length=1, max_length=500)
    delivery_path: str = Field(min_length=1, max_length=512)

    @field_validator("delivery_path")
    @classmethod
    def _first_party_delivery_path_only(cls, value: str) -> str:
        candidate = value.strip()
        if (
            not candidate.startswith(GENERATED_IMAGE_DELIVERY_PREFIX)
            or "?" in candidate
            or "#" in candidate
            or ".." in candidate
            or candidate.endswith("/")
        ):
            raise ValueError("generated_image_delivery_path_invalid")
        return candidate

    @model_validator(mode="after")
    def _delivery_path_matches_asset(self) -> "GeneratedImageAssetRef":
        if self.delivery_path != GENERATED_IMAGE_DELIVERY_PREFIX + self.asset_id:
            raise ValueError("generated_image_delivery_asset_mismatch")
        return self
