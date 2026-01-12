"""
Ella Backend API - Centralized Configuration

All Ella configuration in one place. Override via environment variables.
"""

import os
from dataclasses import dataclass, field
from typing import Optional


@dataclass
class EllaConfig:
    """
    Master configuration for Ella Backend API extensions.

    All settings can be overridden via environment variables.
    """

    # =========================================================================
    # MASTER SWITCHES
    # =========================================================================

    enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_ENABLED", "true").lower() == "true"
    )

    # =========================================================================
    # N8N / LETTA INTEGRATION
    # =========================================================================

    n8n_base_url: str = field(
        default_factory=lambda: os.getenv("ELLA_N8N_BASE_URL", "https://n8n.ella-ai-care.com")
    )

    # Webhook endpoints
    summary_endpoint: str = "/webhook/summary-agent"
    memory_endpoint: str = "/webhook/memory-agent"
    scanner_endpoint: str = "/webhook/scanner-agent"
    voice_init_endpoint: str = "/webhook/voice-init"
    call_state_endpoint: str = "/webhook/call-state"

    # Feature flags
    summary_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_SUMMARY_ENABLED", "true").lower() == "true"
    )
    memory_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_MEMORY_ENABLED", "true").lower() == "true"
    )
    scanner_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_SCANNER_ENABLED", "true").lower() == "true"
    )

    # Timeouts (seconds)
    summary_timeout: float = field(
        default_factory=lambda: float(os.getenv("ELLA_SUMMARY_TIMEOUT", "30.0"))
    )
    memory_timeout: float = field(
        default_factory=lambda: float(os.getenv("ELLA_MEMORY_TIMEOUT", "30.0"))
    )
    scanner_timeout: float = field(
        default_factory=lambda: float(os.getenv("ELLA_SCANNER_TIMEOUT", "2.0"))
    )

    # =========================================================================
    # VOICE V2 (GROK V2V)
    # =========================================================================

    voice_v2_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_VOICE_V2_ENABLED", "true").lower() == "true"
    )

    grok_proxy_url: str = field(
        default_factory=lambda: os.getenv("GROK_V2V_PROXY_URL", "wss://voice.ella-ai-care.com/ws")
    )

    grok_model: str = field(
        default_factory=lambda: os.getenv("GROK_MODEL", "grok-4-1-fast-non-reasoning")
    )

    grok_use_raw_audio: bool = field(
        default_factory=lambda: os.getenv("GROK_USE_RAW_AUDIO", "true").lower() == "true"
    )

    # =========================================================================
    # NOTIFICATIONS
    # =========================================================================

    notifications_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_NOTIFICATIONS_ENABLED", "true").lower() == "true"
    )

    # =========================================================================
    # TESTING / DEVELOPMENT
    # =========================================================================

    testing_enabled: bool = field(
        default_factory=lambda: os.getenv("ELLA_TESTING_ENABLED", "false").lower() == "true"
    )

    # =========================================================================
    # COMPUTED PROPERTIES
    # =========================================================================

    @property
    def summary_url(self) -> str:
        return f"{self.n8n_base_url}{self.summary_endpoint}"

    @property
    def memory_url(self) -> str:
        return f"{self.n8n_base_url}{self.memory_endpoint}"

    @property
    def scanner_url(self) -> str:
        return f"{self.n8n_base_url}{self.scanner_endpoint}"

    @property
    def voice_init_url(self) -> str:
        return f"{self.n8n_base_url}{self.voice_init_endpoint}"

    @property
    def call_state_url(self) -> str:
        return f"{self.n8n_base_url}{self.call_state_endpoint}"


# Global config instance - import this
ELLA_CONFIG = EllaConfig()


def is_ella_enabled() -> bool:
    """Check if Ella is enabled."""
    return ELLA_CONFIG.enabled


def get_config() -> EllaConfig:
    """Get the global Ella config."""
    return ELLA_CONFIG


# For backward compatibility with utils/ella/config.py
__all__ = [
    'EllaConfig',
    'ELLA_CONFIG',
    'is_ella_enabled',
    'get_config',
]
