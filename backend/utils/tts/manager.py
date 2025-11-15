"""
TTS Manager

Handles provider selection, caching, and A/B testing.
"""

import hashlib
import json
import os
from datetime import datetime, timedelta
from typing import Optional, Dict, Any
from google.cloud import storage, firestore
import redis

from utils.tts.base import (
    BaseTTSProvider,
    TTSRequest,
    TTSResponse,
    TTSGenerationError
)
from utils.tts.providers.openai_provider import OpenAITTSProvider
from utils.tts.providers.coqui_provider import CoquiTTSProvider


class TTSManager:
    """
    Manages TTS generation with provider abstraction,
    caching, and A/B testing.
    """

    def __init__(
        self,
        storage_bucket: str,
        redis_client: Optional[redis.Redis] = None,
        firestore_client: Optional[firestore.Client] = None
    ):
        """
        Initialize TTS Manager.

        Args:
            storage_bucket: GCS bucket name for audio storage
            redis_client: Redis client for caching (optional)
            firestore_client: Firestore client for metadata (optional)
        """
        self.storage_bucket = storage_bucket
        self.redis = redis_client
        self.db = firestore_client

        # Initialize storage client
        self.storage_client = storage.Client()
        self.bucket = self.storage_client.bucket(storage_bucket)

        # Initialize providers
        self.providers: Dict[str, BaseTTSProvider] = {}
        self._init_providers()

        # Default provider (can be changed via env var)
        self.default_provider = os.getenv("DEFAULT_TTS_PROVIDER", "openai")

        # Cache TTL
        self.cache_ttl_days = int(os.getenv("TTS_CACHE_TTL_DAYS", "30"))

    def _init_providers(self):
        """Initialize all available TTS providers"""

        # OpenAI provider (always available if API key exists)
        openai_key = os.getenv("OPENAI_API_KEY")
        if openai_key:
            self.providers["openai"] = OpenAITTSProvider({"api_key": openai_key})
            print("✅ OpenAI TTS provider initialized")

        # Coqui provider (self-hosted on M4 Mac)
        coqui_url = os.getenv("COQUI_TTS_URL", "http://m4-mac.tailscale:5000")
        if os.getenv("ENABLE_COQUI_TTS", "false").lower() == "true":
            self.providers["coqui"] = CoquiTTSProvider({"base_url": coqui_url})
            print(f"✅ Coqui TTS provider initialized (endpoint: {coqui_url})")

        if not self.providers:
            print("⚠️ WARNING: No TTS providers configured!")

    def get_provider(self, provider_name: Optional[str] = None) -> BaseTTSProvider:
        """
        Get TTS provider by name.

        Args:
            provider_name: Provider name ('openai', 'coqui', etc.)
                          If None, uses default provider

        Returns:
            BaseTTSProvider instance

        Raises:
            ValueError: If provider not found
        """
        name = provider_name or self.default_provider

        if name not in self.providers:
            available = list(self.providers.keys())
            raise ValueError(
                f"TTS provider '{name}' not available. "
                f"Available providers: {available}"
            )

        return self.providers[name]

    async def generate(
        self,
        request: TTSRequest,
        provider_name: Optional[str] = None,
        uid: Optional[str] = None
    ) -> TTSResponse:
        """
        Generate TTS audio with caching and provider selection.

        Args:
            request: TTSRequest with text, voice, model, etc.
            provider_name: Provider to use (default: from env var)
            uid: User ID for analytics/quota tracking

        Returns:
            TTSResponse with audio URL and metadata

        Raises:
            TTSGenerationError: If generation fails
        """

        # Check cache first
        cache_key = self._get_cache_key(request, provider_name)
        cached_response = self._get_cached_response(cache_key)
        if cached_response:
            print(f"TTS cache hit: {request.text[:50]}...")
            cached_response.cached = True
            return cached_response

        # Get provider
        provider = self.get_provider(provider_name)

        # Generate audio
        try:
            audio_bytes = await provider.generate(request)
        except TTSGenerationError:
            # If primary provider fails, try fallback (OpenAI)
            if provider_name != "openai" and "openai" in self.providers:
                print(f"⚠️ {provider_name} failed, falling back to OpenAI")
                provider = self.providers["openai"]
                audio_bytes = await provider.generate(request)
            else:
                raise

        # Upload to storage
        audio_url = await self._upload_audio(cache_key, audio_bytes)

        # Create response
        response = TTSResponse(
            audio_url=audio_url,
            duration_ms=self._estimate_duration_ms(len(audio_bytes)),
            cached=False,
            voice=request.voice.value,
            provider=provider.get_provider_name(),
            size_bytes=len(audio_bytes),
            expires_at=(datetime.utcnow() + timedelta(days=self.cache_ttl_days)).isoformat(),
            model=request.model.value
        )

        # Cache response
        self._cache_response(cache_key, response, request)

        # Track analytics
        if uid:
            self._track_usage(uid, provider.get_provider_name(), request, response)

        return response

    def _get_cache_key(self, request: TTSRequest, provider_name: Optional[str]) -> str:
        """Generate cache key from request"""
        provider = provider_name or self.default_provider
        text_hash = hashlib.md5(request.text.encode()).hexdigest()

        # Include custom cache_key if provided
        if request.cache_key:
            return f"tts:{provider}:{request.voice.value}:{request.cache_key}"

        return f"tts:{provider}:{request.voice.value}:{request.model.value}:{text_hash}"

    def _get_cached_response(self, cache_key: str) -> Optional[TTSResponse]:
        """Get cached response from Redis"""
        if not self.redis:
            return None

        try:
            cached_json = self.redis.get(cache_key)
            if cached_json:
                return TTSResponse(**json.loads(cached_json))
        except Exception as e:
            print(f"Cache get error: {e}")

        return None

    def _cache_response(self, cache_key: str, response: TTSResponse, request: TTSRequest):
        """Cache response in Redis and Firestore"""

        # Redis cache (fast lookup)
        if self.redis:
            try:
                self.redis.setex(
                    cache_key,
                    self.cache_ttl_days * 24 * 60 * 60,  # seconds
                    response.json()
                )
            except Exception as e:
                print(f"Cache set error: {e}")

        # Firestore metadata (analytics)
        if self.db:
            try:
                self.db.collection('tts_cache').document(cache_key).set({
                    'text': request.text,
                    'voice': request.voice.value,
                    'model': request.model.value,
                    'provider': response.provider,
                    'audio_url': response.audio_url,
                    'size_bytes': response.size_bytes,
                    'created_at': datetime.utcnow(),
                    'access_count': 1,
                    'cache_key': request.cache_key,
                    'expires_at': datetime.fromisoformat(response.expires_at.replace('Z', '+00:00'))
                })
            except Exception as e:
                print(f"Firestore metadata save error: {e}")

    async def _upload_audio(self, cache_key: str, audio_bytes: bytes) -> str:
        """Upload audio to GCS and return public URL"""
        # Clean cache key for filename (remove 'tts:' prefix and colons)
        filename = cache_key.replace('tts:', '').replace(':', '_') + '.mp3'
        blob_name = f"tts/{filename}"

        blob = self.bucket.blob(blob_name)
        blob.upload_from_string(audio_bytes, content_type="audio/mpeg")
        blob.make_public()

        return blob.public_url

    def _estimate_duration_ms(self, audio_size_bytes: int) -> int:
        """
        Estimate audio duration from file size.

        Rough estimate for MP3 @ 128kbps:
        128 kbps = 16 KB/s = 16000 bytes/second
        """
        bytes_per_second = 16000
        duration_seconds = audio_size_bytes / bytes_per_second
        return int(duration_seconds * 1000)

    def _track_usage(self, uid: str, provider: str, request: TTSRequest, response: TTSResponse):
        """Track TTS usage for analytics"""
        if not self.db:
            return

        try:
            self.db.collection('tts_usage').add({
                'uid': uid,
                'provider': provider,
                'voice': request.voice.value,
                'model': request.model.value,
                'text_length': len(request.text),
                'audio_size_bytes': response.size_bytes,
                'cached': response.cached,
                'timestamp': datetime.utcnow()
            })
        except Exception as e:
            print(f"Usage tracking error: {e}")

    def get_available_providers(self) -> list[str]:
        """Get list of initialized providers"""
        return list(self.providers.keys())

    def estimate_cost(self, text: str, provider_name: Optional[str] = None) -> float:
        """Estimate generation cost"""
        provider = self.get_provider(provider_name)
        return provider.estimate_cost(text)
