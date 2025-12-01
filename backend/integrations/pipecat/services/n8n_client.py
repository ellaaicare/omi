"""
n8n webhook client for Pipecat integration.

Handles communication with n8n endpoints for:
- Voice configuration (system prompt, memory blocks)
- Memory agent (extract memories from conversation)
- Summary agent (generate conversation summary)
"""

import httpx
from typing import Optional

from ..pipeline.config import N8NConfig


class N8NClient:
    """
    Client for n8n webhook endpoints.

    This client is designed to be passed into processors via dependency
    injection, making the Pipecat integration testable and configurable.
    """

    def __init__(self, config: Optional[N8NConfig] = None):
        """
        Initialize n8n client.

        Args:
            config: n8n configuration (uses defaults if not provided)
        """
        self.config = config or N8NConfig()

    async def fetch_voice_config(self, uid: str) -> dict:
        """
        Fetch voice configuration from n8n.

        Returns agent config, persona, and memory blocks for the user.

        Args:
            uid: Firebase user ID

        Returns:
            Configuration dict with keys:
            - agent_config: LLM settings (model, temperature, etc.)
            - blocks: Memory blocks (user_profile, rolling_memories, etc.)
            - persona: Ella's persona/system prompt base
            - user: User info (name, timezone, etc.)
        """
        async with httpx.AsyncClient(timeout=self.config.timeout_seconds) as client:
            try:
                response = await client.post(
                    self.config.voice_config_url,
                    json={"uid": uid},
                )
                response.raise_for_status()
                config = response.json()
                print(f"✅ Fetched voice config for uid={uid[:8]}...")
                return config

            except httpx.TimeoutException:
                print(f"⚠️ Voice config timeout for uid={uid[:8]}, using defaults")
                return self._default_config()

            except httpx.HTTPStatusError as e:
                print(f"⚠️ Voice config error ({e.response.status_code}), using defaults")
                return self._default_config()

            except Exception as e:
                print(f"⚠️ Voice config failed: {e}, using defaults")
                return self._default_config()

    async def call_memory_agent(
        self,
        uid: str,
        conversation_id: str,
        transcript: str,
        segments: list[dict],
    ) -> dict:
        """
        Call memory agent to extract memories from conversation.

        Args:
            uid: Firebase user ID
            conversation_id: Session/conversation ID
            transcript: Full conversation transcript
            segments: List of conversation turns

        Returns:
            Response from memory agent
        """
        payload = {
            "uid": uid,
            "conversation_id": conversation_id,
            "transcript": transcript,
            "segments": segments,
            "source": "voice_mode_v2",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    self.config.memory_agent_url,
                    json=payload,
                )
                response.raise_for_status()
                print(f"✅ Memory agent called for {conversation_id[:8]}")
                return response.json()

            except Exception as e:
                print(f"⚠️ Memory agent failed: {e}")
                return {"error": str(e)}

    async def call_summary_agent(
        self,
        uid: str,
        conversation_id: str,
        transcript: str,
        segments: list[dict],
    ) -> dict:
        """
        Call summary agent to generate conversation summary.

        Args:
            uid: Firebase user ID
            conversation_id: Session/conversation ID
            transcript: Full conversation transcript
            segments: List of conversation turns

        Returns:
            Response from summary agent
        """
        payload = {
            "uid": uid,
            "conversation_id": conversation_id,
            "transcript": transcript,
            "segments": segments,
            "source": "voice_mode_v2",
        }

        async with httpx.AsyncClient(timeout=30) as client:
            try:
                response = await client.post(
                    self.config.summary_agent_url,
                    json=payload,
                )
                response.raise_for_status()
                print(f"✅ Summary agent called for {conversation_id[:8]}")
                return response.json()

            except Exception as e:
                print(f"⚠️ Summary agent failed: {e}")
                return {"error": str(e)}

    def _default_config(self) -> dict:
        """
        Return default configuration when n8n is unavailable.

        This ensures voice mode works even if n8n is down.
        """
        return {
            "agent_config": {
                "model": "llama-3.3-70b-versatile",
                "provider": "groq",
                "temperature": 0.7,
                "max_tokens": 150,
            },
            "blocks": {
                "user_profile": "No profile available.",
                "rolling_memories": "No recent memories available.",
                "rolling_summaries": "No recent conversations available.",
            },
            "persona": (
                "You are Ella, a warm and caring AI companion. "
                "You help users with daily tasks, offer emotional support, "
                "and engage in friendly conversation. Be concise and natural."
            ),
            "user": {
                "name": "there",
            },
        }
