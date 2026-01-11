"""
Grok Voice-to-Voice Pipeline - Ultra-Low Latency Mode

This pipeline proxies audio to Ella's Grok voice proxy for ~500ms latency
instead of the traditional STT→LLM→TTS pipeline (2-3s latency).

Architecture:
    iOS (16kHz PCM16) → Backend → Resample to 24kHz → Grok Proxy
                                                          ↓
                                                    Grok handles:
                                                    - System prompt (Ella persona)
                                                    - Tool definitions (5 tools)
                                                    - Function calling → n8n
                                                    - User context from Letta
                                                          ↓
                                                    24kHz audio response
                                                          ↓
                                                    iOS playback

The proxy handles all LLM config server-side. Client just sends:
- uid query param
- 24kHz PCM16 audio bytes

Reference: Issue #30 - https://github.com/ellaaicare/omi/issues/30
"""

import asyncio
import struct
import time
import uuid
from typing import Optional
from dataclasses import dataclass
from fastapi import WebSocket, WebSocketDisconnect

import httpx
import websockets

from .config import PipelineConfig, DEFAULT_CONFIG
from ..services.n8n_client import N8NClient


@dataclass
class GrokSessionConfig:
    """Configuration for a Grok V2V session."""
    system_prompt: str = ""
    voice: str = "Cove"  # Grok voice options
    temperature: float = 0.7
    user_name: str = "User"


class GrokVoicePipeline:
    """
    Grok Voice-to-Voice pipeline for ultra-low latency voice interactions.

    This proxies audio between iOS and Grok's voice API, achieving ~500ms
    latency compared to 2-3 seconds with the traditional pipeline.
    """

    def __init__(
        self,
        websocket: WebSocket,
        uid: str,
        session_id: str,
        config: PipelineConfig = None,
    ):
        self.websocket = websocket
        self.uid = uid
        self.session_id = session_id
        self.config = config or DEFAULT_CONFIG

        # Grok WebSocket connection
        self.grok_ws: Optional[websockets.WebSocketClientProtocol] = None

        # n8n client for fetching config and post-call processing
        self.n8n_client: Optional[N8NClient] = None

        # Session state
        self.is_running = False
        self.session_config: Optional[GrokSessionConfig] = None

        # Audio resampling buffer (16kHz → 24kHz)
        self.resample_buffer = bytearray()

        # Conversation tracking for post-call memory extraction
        self.conversation_turns = []

    async def initialize(self) -> bool:
        """Initialize the Grok V2V session.

        The Ella proxy handles all configuration server-side:
        - Grok API key
        - System prompt (Ella persona)
        - Tool definitions
        - User context from Letta

        We just need to connect with uid and stream audio.
        """
        try:
            # Initialize n8n client for call state notifications and post-call processing
            self.n8n_client = N8NClient(self.config.n8n)

            # Notify n8n that call started (for scanner to pause)
            await self.n8n_client.notify_call_start(
                uid=self.uid,
                session_id=self.session_id,
                call_type="grok_v2v",
                initiated_by="user",
            )

            # Connect to Grok proxy - just needs uid, proxy handles everything else
            grok_url = f"{self.config.grok_v2v.proxy_url}?uid={self.uid}"
            print(f"🔌 Connecting to Grok proxy: {grok_url}", flush=True)

            self.grok_ws = await websockets.connect(
                grok_url,
                ping_interval=20,
                ping_timeout=10,
            )

            # Send session configuration for proper turn-taking
            # Required: input_audio_transcription + turn_detection for multi-turn conversations
            import json
            session_config = {
                "type": "session.update",
                "session": {
                    "input_audio_transcription": {
                        "model": "whisper-1"  # Required for user speech detection
                    },
                    "turn_detection": {
                        "type": "server_vad",
                        "threshold": 0.5,
                        "prefix_padding_ms": 300,
                        "silence_duration_ms": 500
                    }
                }
            }
            await self.grok_ws.send(json.dumps(session_config))
            print(f"📋 Sent session config with turn_detection", flush=True)

            print(f"✅ Grok V2V connected for uid={self.uid[:8]}", flush=True)
            return True

        except Exception as e:
            print(f"❌ Grok V2V initialization failed: {e}", flush=True)
            return False

    async def run(self):
        """Run the main audio proxy loop."""
        self.is_running = True

        try:
            # Start concurrent tasks for bidirectional audio
            ios_to_grok_task = asyncio.create_task(self._ios_to_grok())
            grok_to_ios_task = asyncio.create_task(self._grok_to_ios())

            # Wait for either task to complete (disconnection)
            done, pending = await asyncio.wait(
                [ios_to_grok_task, grok_to_ios_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Cancel pending tasks
            for task in pending:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        except Exception as e:
            print(f"❌ Grok V2V run error: {e}", flush=True)

        finally:
            self.is_running = False
            await self.cleanup()

    async def _ios_to_grok(self):
        """Forward audio from iOS to Grok (with resampling).

        Grok's realtime API expects audio in JSON format:
        {"type": "input_audio_buffer.append", "audio": "<base64-pcm16>"}
        """
        import base64
        import json

        try:
            while self.is_running:
                # Receive audio from iOS (16kHz PCM16)
                data = await self.websocket.receive_bytes()

                # Resample 16kHz → 24kHz
                resampled = self._resample_16k_to_24k(data)

                # Forward to Grok in required JSON format
                if self.grok_ws and resampled:
                    audio_event = {
                        "type": "input_audio_buffer.append",
                        "audio": base64.b64encode(resampled).decode('utf-8')
                    }
                    await self.grok_ws.send(json.dumps(audio_event))

        except WebSocketDisconnect:
            print(f"📱 iOS disconnected from Grok V2V", flush=True)
        except Exception as e:
            print(f"⚠️ iOS→Grok error: {e}", flush=True)

    async def _grok_to_ios(self):
        """Forward audio from Grok to iOS.

        Grok sends audio in JSON format:
        {"type": "response.audio.delta", "delta": "<base64-pcm16>"}
        """
        import base64
        import json

        try:
            while self.is_running and self.grok_ws:
                # Receive from Grok (always JSON for realtime API)
                message = await self.grok_ws.recv()

                if isinstance(message, bytes):
                    # Unexpected binary - forward directly (fallback)
                    await self.websocket.send_bytes(message)
                else:
                    # Parse JSON event
                    try:
                        event = json.loads(message)
                        event_type = event.get("type", "")

                        # Handle audio delta - decode and forward to iOS
                        if event_type == "response.audio.delta":
                            audio_b64 = event.get("delta", "")
                            if audio_b64:
                                audio_bytes = base64.b64decode(audio_b64)
                                await self.websocket.send_bytes(audio_bytes)
                        else:
                            # Other events (transcripts, VAD, etc.)
                            await self._handle_grok_event(message)

                    except json.JSONDecodeError:
                        print(f"⚠️ Invalid JSON from Grok: {message[:100]}", flush=True)

        except websockets.ConnectionClosed:
            print(f"🔌 Grok proxy disconnected", flush=True)
        except Exception as e:
            print(f"⚠️ Grok→iOS error: {e}", flush=True)

    async def _handle_grok_event(self, message: str):
        """Handle JSON control messages from Grok proxy.

        Key Grok VAD events (for turn-taking):
        - input_audio_buffer.speech_started: User started speaking
        - input_audio_buffer.speech_stopped: VAD detected silence
        - conversation.item.input_audio_transcription.completed: User speech transcribed
        - response.created: Grok generating response

        Proxy-level events:
        - transcript: Speech-to-text result
        - function_calling: Tool being called
        - function_executed: Tool finished
        - audio_done: AI finished speaking
        - error: Error occurred
        """
        try:
            import json
            event = json.loads(message)
            event_type = event.get("type", "")

            # === Grok VAD events (turn-taking) ===
            if event_type == "input_audio_buffer.speech_started":
                print(f"🎙️ User started speaking", flush=True)

            elif event_type == "input_audio_buffer.speech_stopped":
                print(f"🔇 User stopped speaking (VAD)", flush=True)

            elif event_type == "conversation.item.input_audio_transcription.completed":
                transcript = event.get("transcript", "")
                if transcript:
                    self.conversation_turns.append({
                        "role": "user",
                        "text": transcript,
                        "timestamp": time.time(),
                    })
                    print(f"👤 User: {transcript[:60]}{'...' if len(transcript) > 60 else ''}", flush=True)

            elif event_type == "response.created":
                print(f"🤖 Grok generating response...", flush=True)

            elif event_type == "response.done":
                print(f"✅ Grok response complete", flush=True)

            elif event_type == "response.audio.done":
                print(f"🔊 Grok audio stream complete", flush=True)

            elif event_type == "response.audio_transcript.delta":
                # Streaming transcript of AI speech
                delta = event.get("delta", "")
                if delta:
                    print(f"🗣️ {delta}", end="", flush=True)

            elif event_type == "response.audio_transcript.done":
                transcript = event.get("transcript", "")
                if transcript:
                    self.conversation_turns.append({
                        "role": "assistant",
                        "text": transcript,
                        "timestamp": time.time(),
                    })
                    print(f"\n🤖 Ella said: {transcript[:80]}{'...' if len(transcript) > 80 else ''}", flush=True)

            # === Proxy-level events ===
            elif event_type == "transcript":
                text = event.get("text", "")
                if text:
                    self.conversation_turns.append({
                        "role": "assistant",
                        "text": text,
                        "timestamp": time.time(),
                    })
                    print(f"🤖 Ella: {text[:60]}{'...' if len(text) > 60 else ''}", flush=True)

            elif event_type == "function_calling":
                func_name = event.get("function", "unknown")
                call_id = event.get("call_id", "")[:8]
                print(f"🔧 Calling tool: {func_name} ({call_id})", flush=True)

            elif event_type == "function_executed":
                func_name = event.get("function", "unknown")
                call_id = event.get("call_id", "")[:8]
                print(f"✅ Tool done: {func_name} ({call_id})", flush=True)

            elif event_type == "audio_done":
                print(f"🔊 AI finished speaking", flush=True)

            elif event_type == "error":
                print(f"⚠️ Grok error: {event.get('message', 'Unknown')}", flush=True)

            elif event_type == "session.created":
                print(f"📋 Grok session created", flush=True)

            elif event_type == "session.updated":
                print(f"📋 Grok session updated", flush=True)

            else:
                # Log unknown events for debugging
                print(f"📨 Grok event: {event_type}", flush=True)

        except Exception as e:
            print(f"⚠️ Grok event parse error: {e}", flush=True)

    def _resample_16k_to_24k(self, audio_16k: bytes) -> bytes:
        """
        Resample 16kHz PCM16 to 24kHz PCM16.

        Uses simple linear interpolation (1.5x samples).
        For production, consider using a proper resampler like libsamplerate.
        """
        if len(audio_16k) < 4:
            return b""

        # Parse 16-bit samples
        samples_16k = []
        for i in range(0, len(audio_16k) - 1, 2):
            sample = struct.unpack('<h', audio_16k[i:i+2])[0]
            samples_16k.append(sample)

        if not samples_16k:
            return b""

        # Linear interpolation to 1.5x samples (16k → 24k)
        samples_24k = []
        for i in range(len(samples_16k) - 1):
            s0 = samples_16k[i]
            s1 = samples_16k[i + 1]

            # Original sample
            samples_24k.append(s0)
            # Interpolated sample at 1/3
            samples_24k.append(int(s0 * 0.67 + s1 * 0.33))
            # Interpolated sample at 2/3
            samples_24k.append(int(s0 * 0.33 + s1 * 0.67))

        # Add last sample
        if samples_16k:
            samples_24k.append(samples_16k[-1])

        # Pack back to bytes
        result = bytearray()
        for sample in samples_24k:
            # Clamp to int16 range
            sample = max(-32768, min(32767, sample))
            result.extend(struct.pack('<h', sample))

        return bytes(result)

    async def cleanup(self):
        """Clean up resources and trigger post-call processing."""
        print(f"🧹 Cleaning up Grok V2V session {self.session_id[:8]}", flush=True)

        # Close Grok WebSocket
        if self.grok_ws:
            try:
                await self.grok_ws.close()
            except:
                pass
            self.grok_ws = None

        # Notify n8n that call ended
        if self.n8n_client:
            await self.n8n_client.notify_call_end(
                uid=self.uid,
                session_id=self.session_id,
                ended_by="user",
                status="completed",
            )

            # Trigger memory extraction if we have conversation data
            if self.conversation_turns:
                await self._trigger_post_call_processing()

    async def _trigger_post_call_processing(self):
        """Trigger memory and summary agents for post-call processing."""
        if not self.conversation_turns:
            return

        # Build transcript from turns
        transcript = "\n".join([
            f"{turn['role'].upper()}: {turn['text']}"
            for turn in self.conversation_turns
        ])

        # Convert to segments format expected by n8n
        segments = [
            {
                "text": turn["text"],
                "speaker": "SPEAKER_00" if turn["role"] == "user" else "SPEAKER_01",
                "start": turn.get("timestamp", 0),
                "end": turn.get("timestamp", 0) + 1,
                "role": turn["role"],
            }
            for turn in self.conversation_turns
        ]

        try:
            # Call memory agent (fire-and-forget)
            asyncio.create_task(
                self.n8n_client.call_memory_agent(
                    uid=self.uid,
                    conversation_id=self.session_id,
                    transcript=transcript,
                    segments=segments,
                )
            )

            # Call summary agent (fire-and-forget)
            asyncio.create_task(
                self.n8n_client.call_summary_agent(
                    uid=self.uid,
                    conversation_id=self.session_id,
                    transcript=transcript,
                    segments=segments,
                )
            )

            print(f"📤 Triggered post-call processing for {self.session_id[:8]}", flush=True)

        except Exception as e:
            print(f"⚠️ Post-call processing error: {e}", flush=True)


async def run_grok_v2v_session(
    websocket: WebSocket,
    uid: str,
    session_id: str,
    config: PipelineConfig = None,
):
    """
    Run a Grok Voice-to-Voice session.

    This is the entry point called from voice_v2.py router.
    """
    pipeline = GrokVoicePipeline(
        websocket=websocket,
        uid=uid,
        session_id=session_id,
        config=config,
    )

    if await pipeline.initialize():
        await pipeline.run()
    else:
        print(f"❌ Failed to initialize Grok V2V for {uid}", flush=True)
        try:
            await websocket.close(code=1011, reason="Failed to initialize Grok V2V")
        except:
            pass
