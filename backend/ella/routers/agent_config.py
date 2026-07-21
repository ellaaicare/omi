from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict

from ella.services.agent_config import get_agent_config, patch_agent_config
from utils.other import endpoints as auth

router = APIRouter(prefix="/v1/ella", tags=["Ella Agent Config"])


class AgentConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str
    model: str


@router.get("/agent-config")
async def get_current_agent_config(uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Return the authenticated user's active Hermes agent model config."""
    return await get_agent_config(uid)


@router.patch("/agent-config")
async def patch_current_agent_config(
    payload: AgentConfigPatch,
    uid: str = Depends(auth.get_current_user_uid),
) -> dict[str, Any]:
    """Update only provider/model for the authenticated user's active Hermes profile."""
    return await patch_agent_config(uid, payload.provider, payload.model)
