from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from database import app_settings as app_settings_db
from ella.services.app_settings import (
    build_effective_voice_settings,
    build_settings_response,
    extract_voice_settings,
)
from utils.other import endpoints as auth

router = APIRouter(prefix="/v1/ella/settings", tags=["Ella Settings"])


@router.get("")
def get_settings(uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Return server-backed Ella app settings for the authenticated user."""
    voice = app_settings_db.get_voice_settings(uid)
    return build_settings_response(uid, voice)


@router.patch("")
def patch_settings(payload: dict[str, Any], uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Persist app settings from iOS. First slice stores effective voice mode."""
    voice = extract_voice_settings(payload)
    saved_voice = app_settings_db.save_voice_settings(uid, voice)
    return build_settings_response(uid, saved_voice)


@router.get("/effective")
def get_effective_settings(uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Return settings plus backend-resolved routing fields for services like Guardian."""
    voice = app_settings_db.get_voice_settings(uid)
    return build_effective_voice_settings(uid, voice)


@router.get("/effective/voice")
def get_effective_voice_settings(uid: str = Depends(auth.get_current_user_uid)) -> dict[str, Any]:
    """Return only the effective voice resolver payload."""
    voice = app_settings_db.get_voice_settings(uid)
    return build_effective_voice_settings(uid, voice)["effective_voice_settings"]
