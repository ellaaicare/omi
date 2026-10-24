"""
Ella Caregiver Prompt Overrides

Loads caregiver-enhanced prompts from sidecar files.
Falls back to upstream prompts if files are missing or feature is disabled.
"""

import os
from pathlib import Path
from typing import Optional
from functools import lru_cache

from langchain_core.prompts import ChatPromptTemplate

PROMPTS_DIR = Path(__file__).parent
ELLA_CAREGIVER_PROMPTS_ENABLED = os.getenv('ELLA_CAREGIVER_PROMPTS_ENABLED', 'true').lower() == 'true'


def _load_prompt_file(filename: str) -> Optional[str]:
    """Load a prompt override file. Returns None if file doesn't exist."""
    filepath = PROMPTS_DIR / filename
    if filepath.exists():
        return filepath.read_text(encoding='utf-8').strip()
    return None


@lru_cache(maxsize=8)
def _cached_prompt(filename: str, mtime: float) -> Optional[str]:
    """Cache prompt text keyed by filename + modification time.
    mtime param busts cache when file is edited."""
    return _load_prompt_file(filename)


def get_prompt(filename: str) -> Optional[str]:
    """Get a prompt override, with file-modification-aware caching.
    Returns None if caregiver prompts are disabled or file is missing."""
    if not ELLA_CAREGIVER_PROMPTS_ENABLED:
        return None

    filepath = PROMPTS_DIR / filename
    if not filepath.exists():
        return None

    mtime = filepath.stat().st_mtime
    return _cached_prompt(filename, mtime)


def get_structured_summary_prompt() -> Optional[ChatPromptTemplate]:
    """Get caregiver-enhanced structured summary prompt."""
    text = get_prompt('structured_summary.txt')
    if text:
        return ChatPromptTemplate.from_messages([('system', text)])
    return None


def get_action_items_prompt() -> Optional[ChatPromptTemplate]:
    """Get caregiver-enhanced action items prompt."""
    text = get_prompt('action_items.txt')
    if text:
        return ChatPromptTemplate.from_messages([('system', text)])
    return None


def get_memory_extraction_prompt() -> Optional[ChatPromptTemplate]:
    """Get caregiver-enhanced memory extraction prompt."""
    text = get_prompt('memory_extraction.txt')
    if text:
        return ChatPromptTemplate.from_messages([text])
    return None


def should_use_caregiver_prompts(uid: str = None) -> bool:
    """Check if caregiver prompts should be used for this user.

    Currently returns True for all users if ELLA_CAREGIVER_PROMPTS_ENABLED=true.
    Future: check user's guardian_mode setting for per-user opt-in.
    """
    if not ELLA_CAREGIVER_PROMPTS_ENABLED:
        return False
    return True
