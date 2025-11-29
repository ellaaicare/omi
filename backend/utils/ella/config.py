# Ella AI Configuration
#
# Centralized configuration for all Ella/n8n integration.
# Modify endpoints, timeouts, and feature flags here.

import os
from dataclasses import dataclass
from typing import Optional


@dataclass
class EllaConfig:
    """Configuration for Ella AI integration."""

    # Base URL for n8n webhooks
    n8n_base_url: str = "https://n8n.ella-ai-care.com"

    # Webhook endpoints
    scanner_endpoint: str = "/webhook/scanner-agent"
    summary_endpoint: str = "/webhook/summary-agent"
    memory_endpoint: str = "/webhook/memory-agent"
    voice_stream_endpoint: str = "/webhook/voice-stream"  # Future: voice mode

    # Timeouts (seconds)
    scanner_timeout: float = 2.0  # Fire-and-forget, fast
    summary_timeout: float = 30.0  # Sync call, may take time
    memory_timeout: float = 30.0  # Sync call, may take time
    voice_timeout: float = 60.0  # Streaming, longer

    # Feature flags
    ella_only_mode: bool = True  # If True, don't fall back to local LLM
    scanner_enabled: bool = True  # Enable real-time scanning
    async_mode: bool = True  # Use async callbacks instead of sync

    @property
    def scanner_url(self) -> str:
        return f"{self.n8n_base_url}{self.scanner_endpoint}"

    @property
    def summary_url(self) -> str:
        return f"{self.n8n_base_url}{self.summary_endpoint}"

    @property
    def memory_url(self) -> str:
        return f"{self.n8n_base_url}{self.memory_endpoint}"

    @property
    def voice_stream_url(self) -> str:
        return f"{self.n8n_base_url}{self.voice_stream_endpoint}"


def load_config() -> EllaConfig:
    """Load Ella configuration from environment variables."""
    config = EllaConfig()

    # Override from environment
    if os.getenv('ELLA_N8N_BASE_URL'):
        config.n8n_base_url = os.getenv('ELLA_N8N_BASE_URL')

    if os.getenv('ELLA_ONLY_MODE'):
        config.ella_only_mode = os.getenv('ELLA_ONLY_MODE', 'true').lower() == 'true'

    if os.getenv('ELLA_SCANNER_ENABLED'):
        config.scanner_enabled = os.getenv('ELLA_SCANNER_ENABLED', 'true').lower() == 'true'

    if os.getenv('ELLA_ASYNC_MODE'):
        config.async_mode = os.getenv('ELLA_ASYNC_MODE', 'true').lower() == 'true'

    return config


# Global config instance
ELLA_CONFIG = load_config()


def is_ella_enabled() -> bool:
    """Check if Ella integration is enabled."""
    return ELLA_CONFIG.scanner_enabled or not ELLA_CONFIG.ella_only_mode
