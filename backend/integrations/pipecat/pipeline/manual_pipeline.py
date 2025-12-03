"""
Manual Voice Pipeline - Option B Implementation

This bypasses Pipecat's FastAPIWebsocketTransport (designed for telephony)
and handles raw WebSocket audio directly, which is how Vapi and Gemini do it.

Architecture:
    iOS → Raw PCM16 16kHz → FastAPI WebSocket → This Pipeline
                                                    ↓
                                            Silero VAD
                                                    ↓
                                            Deepgram STT (streaming)
                                                    ↓
                                            Groq LLM
                                                    ↓
                                            OpenAI TTS
                                                    ↓
                                            PCM16 audio back to iOS
"""

import asyncio
import time
import uuid
from typing import Optional
from dataclasses import dataclass
from fastapi import WebSocket

# Pipecat services (we use these directly, not the transport)
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.audio.vad.vad_analyzer import VADParams, VADState

# External APIs
import httpx
from openai import AsyncOpenAI

# ElevenLabs for true streaming TTS
try:
    from elevenlabs import AsyncElevenLabs
    ELEVENLABS_AVAILABLE = True
except ImportError:
    ELEVENLABS_AVAILABLE = False

from .config import PipelineConfig, DEFAULT_CONFIG
from ..services.n8n_client import N8NClient
from ..services.firestore_client import FirestoreClient


@dataclass
class AudioChunk:
    """A chunk of audio with metadata."""
    data: bytes
    timestamp: float
    sample_rate: int = 16000


@dataclass
class ConversationTurn:
    """A single turn in the conversation."""
    role: str  # "user" or "assistant"
    text: str
    timestamp: float


class ManualVoicePipeline:
    """
    Manual voice pipeline that handles raw WebSocket audio.

    This is the industry-standard approach used by Vapi and Gemini.
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

        # Services
        self.openai_client: Optional[AsyncOpenAI] = None
        self.elevenlabs_client = None  # Optional[AsyncElevenLabs]
        self.http_client: Optional[httpx.AsyncClient] = None
        self.n8n_client: Optional[N8NClient] = None
        self.firestore_client: Optional[FirestoreClient] = None

        # VAD
        self.vad: Optional[SileroVADAnalyzer] = None
        self.vad_state = VADState.QUIET

        # Audio buffer for VAD processing
        self.audio_buffer = bytearray()
        self.speaking_audio = bytearray()  # Audio collected while speaking

        # Conversation state
        self.turns: list[ConversationTurn] = []
        self.system_prompt: str = ""
        self.is_running = False
        self.last_speech_time: float = 0
        self.is_speaking_tts = False  # Track when AI is speaking for barge-in
        self.barge_in_detected = False  # User interrupted AI
        self.tts_task: Optional[asyncio.Task] = None  # Background TTS task for cancellation

        # Stats
        self.chunks_received = 0
        self.bytes_received = 0
        self.start_time: float = 0

    async def initialize(self):
        """Initialize services and fetch Ella config."""
        self.config.validate()

        # Initialize OpenAI client for TTS
        self.openai_client = AsyncOpenAI(api_key=self.config.tts.api_key)

        # Initialize ElevenLabs client if available and configured
        if ELEVENLABS_AVAILABLE and self.config.tts.elevenlabs_api_key:
            self.elevenlabs_client = AsyncElevenLabs(api_key=self.config.tts.elevenlabs_api_key)
            print(f"✅ ElevenLabs TTS initialized (voice_id: {self.config.tts.elevenlabs_voice_id})", flush=True)
        elif self.config.tts.provider == "elevenlabs":
            print(f"⚠️ ElevenLabs requested but not available (installed={ELEVENLABS_AVAILABLE}, key_set={bool(self.config.tts.elevenlabs_api_key)})", flush=True)

        # Initialize HTTP client for Deepgram and Groq
        self.http_client = httpx.AsyncClient(timeout=30.0)

        # Initialize n8n client
        self.n8n_client = N8NClient(self.config.n8n)

        # Initialize Firestore client
        self.firestore_client = FirestoreClient()

        # Initialize Silero VAD with proper parameters
        # Silero uses neural network for speech detection - handles background noise properly
        self.vad = SileroVADAnalyzer(
            sample_rate=16000,  # MUST set sample rate for Silero
            params=VADParams(
                confidence=0.6,  # 60% confidence threshold (default 0.7)
                start_secs=0.2,  # 200ms to confirm speech start
                stop_secs=self.config.vad.stop_secs,  # 1.5s silence to stop
                min_volume=0.4,  # 40% min volume (handles quiet speech)
            )
        )
        # Explicitly set sample rate (required by Silero)
        self.vad.set_sample_rate(16000)

        # Silero needs exactly 512 samples (1024 bytes) per call
        self.vad_frame_size = self.vad.num_frames_required() * 2  # 512 samples * 2 bytes = 1024
        print(f"🎯 Silero VAD initialized: frame_size={self.vad_frame_size} bytes", flush=True)

        # VAD timing state
        self.speech_start_time: float = 0
        self.silence_start_time: float = 0
        self.is_speech_confirmed = False

        # Fetch Ella config to get persona and memory context
        try:
            ella_config = await self.n8n_client.fetch_voice_config(self.uid)
            self.system_prompt = self._build_system_prompt(ella_config)
            print(f"✅ Loaded Ella config for uid={self.uid}")
        except Exception as e:
            print(f"⚠️ Failed to fetch Ella config: {e}, using default")
            self.system_prompt = self._default_system_prompt()

        self.start_time = time.time()
        self.is_running = True

        print(f"🎙️ Manual pipeline initialized: session={self.session_id[:8]}", flush=True)

    async def run(self):
        """Main loop - receive audio, process, respond."""
        try:
            while self.is_running:
                try:
                    # Receive data from WebSocket
                    data = await asyncio.wait_for(
                        self.websocket.receive(),
                        timeout=60.0  # 1 minute timeout
                    )

                    # Check for disconnect
                    if data.get("type") == "websocket.disconnect":
                        print(f"🔌 Client disconnected: {self.session_id[:8]}", flush=True)
                        break

                    if "bytes" in data:
                        # Binary audio data
                        await self._process_audio(data["bytes"])

                    elif "text" in data:
                        # JSON control message
                        await self._process_control(data["text"])

                except asyncio.TimeoutError:
                    print(f"⏰ Session timeout: {self.session_id[:8]}", flush=True)
                    break

        except Exception as e:
            # Ignore disconnect errors during cleanup
            if "disconnect" not in str(e).lower():
                print(f"❌ Pipeline error: {e}", flush=True)
                raise

        finally:
            # Process any remaining audio in buffer before cleanup
            if len(self.speaking_audio) > 0:
                print(f"🔄 Processing remaining audio buffer ({len(self.speaking_audio)} bytes)", flush=True)
                await self._process_utterance(bytes(self.speaking_audio))
            await self.cleanup()

    async def _process_audio(self, audio_bytes: bytes):
        """Process incoming audio chunk."""
        self.chunks_received += 1
        self.bytes_received += len(audio_bytes)

        # Log every 50th chunk for debugging (reduced spam)
        if self.chunks_received % 50 == 1:
            print(f"🔊 Chunk #{self.chunks_received}: {len(audio_bytes)} bytes (total: {self.bytes_received / 1024:.1f}KB)", flush=True)

        # Add to buffer
        self.audio_buffer.extend(audio_bytes)

        # Process VAD on chunks - Silero needs exactly 1024 bytes (512 samples @ 16kHz)
        # This is ~32ms of audio per VAD call
        while len(self.audio_buffer) >= self.vad_frame_size:
            chunk = bytes(self.audio_buffer[:self.vad_frame_size])
            del self.audio_buffer[:self.vad_frame_size]

            # Run Silero VAD - returns "speaking", "quiet", or "stopped"
            vad_result = await self._check_vad(chunk)

            if vad_result == "speaking":
                # Speech detected - accumulate audio
                self.speaking_audio.extend(chunk)
                self.last_speech_time = time.time()

            elif vad_result == "quiet":
                # Quiet chunk - but may still need to accumulate if in SPEAKING/STOPPING
                if self.vad_state in (VADState.SPEAKING, VADState.STOPPING):
                    # Keep accumulating audio during pauses to capture complete utterance
                    self.speaking_audio.extend(chunk)

            elif vad_result == "stopped":
                # User stopped speaking - process the utterance
                if len(self.speaking_audio) > 0:
                    duration_ms = len(self.speaking_audio) / 32  # 32 bytes per ms @ 16kHz
                    print(f"🎤 End of speech: {len(self.speaking_audio)} bytes ({duration_ms:.0f}ms)", flush=True)
                    await self._process_utterance(bytes(self.speaking_audio))
                    self.speaking_audio.clear()

    async def _check_vad(self, audio_chunk: bytes) -> str:
        """
        Check VAD state for audio chunk using Silero neural network.

        Silero VAD is trained to distinguish speech from background noise,
        including HVAC, traffic, keyboard typing, etc.

        Returns:
            "speaking" - Speech detected and confirmed
            "quiet" - No speech detected
            "stopped" - Speech ended (was speaking, now quiet for stop_secs)
        """
        # Get voice confidence from Silero neural network (0.0 to 1.0)
        # Silero handles the int16 -> float32 conversion internally
        confidence_raw = self.vad.voice_confidence(audio_chunk)
        # Ensure confidence is a plain float (not numpy array)
        confidence = float(confidence_raw) if hasattr(confidence_raw, '__float__') else confidence_raw

        # Log confidence periodically (every 50 chunks = ~1.6 seconds)
        if not hasattr(self, '_vad_chunk_count'):
            self._vad_chunk_count = 0
        self._vad_chunk_count += 1
        if self._vad_chunk_count % 50 == 1:
            state_name = self.vad_state.name if hasattr(self.vad_state, 'name') else str(self.vad_state)
            print(f"📊 Silero VAD: confidence={confidence:.3f} state={state_name}", flush=True)

        # Get VAD params
        confidence_threshold = float(self.vad._params.confidence)  # 0.6
        start_secs = self.vad._params.start_secs  # 0.2
        stop_secs = self.vad._params.stop_secs  # 1.5

        current_time = time.time()

        # State machine for speech detection
        if confidence >= confidence_threshold:
            # Speech detected
            if self.vad_state == VADState.QUIET:
                # Start tracking potential speech
                self.speech_start_time = current_time
                self.vad_state = VADState.STARTING
                return "quiet"  # Not confirmed yet

            elif self.vad_state == VADState.STARTING:
                # Check if speech sustained long enough to confirm
                if (current_time - self.speech_start_time) >= start_secs:
                    print(f"🗣️ Speech confirmed (confidence: {confidence:.2f})", flush=True)
                    self.vad_state = VADState.SPEAKING
                    self.is_speech_confirmed = True
                    # Barge-in: User started speaking while AI is talking
                    if self.is_speaking_tts:
                        print(f"🎤 User interrupting AI (barge-in)", flush=True)
                        self.barge_in_detected = True
                        # Cancel TTS task
                        if self.tts_task and not self.tts_task.done():
                            self.tts_task.cancel()
                    return "speaking"
                return "quiet"  # Still confirming

            elif self.vad_state == VADState.SPEAKING:
                # Continue speaking
                self.silence_start_time = 0  # Reset silence timer
                return "speaking"

            elif self.vad_state == VADState.STOPPING:
                # Was stopping, but speech resumed
                print(f"🗣️ Speech resumed (confidence: {confidence:.2f})", flush=True)
                self.vad_state = VADState.SPEAKING
                self.silence_start_time = 0
                return "speaking"

        else:
            # Silence detected
            if self.vad_state == VADState.QUIET:
                return "quiet"

            elif self.vad_state == VADState.STARTING:
                # Speech not confirmed, back to quiet
                self.vad_state = VADState.QUIET
                return "quiet"

            elif self.vad_state == VADState.SPEAKING:
                # Start tracking silence
                self.silence_start_time = current_time
                self.vad_state = VADState.STOPPING
                return "quiet"  # Still accumulating audio

            elif self.vad_state == VADState.STOPPING:
                # Check if silence sustained long enough to stop
                if (current_time - self.silence_start_time) >= stop_secs:
                    print(f"🤫 Speech stopped after {stop_secs}s silence", flush=True)
                    self.vad_state = VADState.QUIET
                    self.is_speech_confirmed = False
                    return "stopped"
                return "quiet"  # Still accumulating audio

        return "quiet"

    async def _process_utterance(self, audio_bytes: bytes):
        """Process a complete user utterance through the pipeline."""
        try:
            # ====== LATENCY TRACKING ======
            t_start = time.time()
            timings = {}

            # 1. STT - Deepgram
            t_stt_start = time.time()
            print("📝 Transcribing with Deepgram...", flush=True)
            transcript = await self._transcribe_deepgram(audio_bytes)
            t_stt_end = time.time()
            timings['stt'] = (t_stt_end - t_stt_start) * 1000  # ms

            if not transcript or not transcript.strip():
                print("⚠️ Empty transcript, skipping", flush=True)
                return

            print(f"👤 User: {transcript}", flush=True)
            print(f"⏱️ STT latency: {timings['stt']:.0f}ms", flush=True)

            # Record user turn
            self.turns.append(ConversationTurn(
                role="user",
                text=transcript,
                timestamp=time.time()
            ))

            # Add thinking pause (200-300ms) for natural conversation feel
            t_pause_start = time.time()
            await asyncio.sleep(0.25)  # 250ms thinking pause
            timings['pause'] = (time.time() - t_pause_start) * 1000

            # 2. LLM - Groq
            t_llm_start = time.time()
            print("🤖 Generating response with Groq...", flush=True)
            response = await self._generate_response_groq(transcript)
            t_llm_end = time.time()
            timings['llm'] = (t_llm_end - t_llm_start) * 1000  # ms

            if not response:
                response = "I'm sorry, I couldn't process that. Could you repeat?"

            print(f"🤖 Assistant: {response}", flush=True)
            print(f"⏱️ LLM latency: {timings['llm']:.0f}ms ({len(response)} chars)", flush=True)

            # Record assistant turn
            self.turns.append(ConversationTurn(
                role="assistant",
                text=response,
                timestamp=time.time()
            ))

            # 3. TTS - OpenAI (run in background for barge-in support)
            t_tts_start = time.time()
            print("🔊 Generating speech with OpenAI TTS...", flush=True)
            # Cancel any existing TTS task
            if self.tts_task and not self.tts_task.done():
                self.tts_task.cancel()
            # Spawn TTS as background task - DON'T await it!
            # This allows main loop to continue receiving audio for barge-in detection
            self.tts_task = asyncio.create_task(self._speak_response_timed(response, t_tts_start, timings, t_start))

        except Exception as e:
            print(f"❌ Utterance processing error: {e}")
            # Try to speak an error message
            try:
                await self._speak_response("Sorry, I had trouble understanding that.")
            except:
                pass

    async def _transcribe_deepgram(self, audio_bytes: bytes) -> str:
        """Transcribe audio using Deepgram REST API."""
        # Debug: Log audio size and duration
        duration_secs = len(audio_bytes) / (16000 * 2)  # 16kHz, 16-bit
        print(f"📊 Deepgram input: {len(audio_bytes)} bytes ({duration_secs:.2f}s)", flush=True)

        url = "https://api.deepgram.com/v1/listen"
        params = {
            "model": self.config.stt.model,
            "language": self.config.stt.language,
            "encoding": "linear16",
            "sample_rate": "16000",
            "channels": "1",
        }
        headers = {
            "Authorization": f"Token {self.config.stt.api_key}",
            "Content-Type": "audio/raw",
        }

        response = await self.http_client.post(
            url,
            params=params,
            headers=headers,
            content=audio_bytes,
        )

        if response.status_code != 200:
            print(f"❌ Deepgram error: {response.status_code} - {response.text}", flush=True)
            return ""

        result = response.json()

        # Debug: Log full Deepgram response
        import json
        print(f"📋 Deepgram response: {json.dumps(result, indent=2)[:500]}...", flush=True)

        # Extract transcript
        try:
            alt = result["results"]["channels"][0]["alternatives"][0]
            transcript = alt.get("transcript", "")
            confidence = alt.get("confidence", 0)
            print(f"📝 Deepgram result: '{transcript}' (confidence: {confidence:.2f})", flush=True)
            return transcript
        except (KeyError, IndexError) as e:
            print(f"⚠️ Deepgram parse error: {e}, result keys: {result.keys()}", flush=True)
            return ""

    async def _generate_response_groq(self, user_message: str) -> str:
        """Generate response using Groq LLM."""
        url = "https://api.groq.com/openai/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.config.llm.api_key}",
            "Content-Type": "application/json",
        }

        # Build messages with history
        messages = [{"role": "system", "content": self.system_prompt}]

        # Add conversation history (last 10 turns)
        for turn in self.turns[-10:]:
            messages.append({
                "role": turn.role,
                "content": turn.text,
            })

        # Add current user message
        messages.append({
            "role": "user",
            "content": user_message,
        })

        payload = {
            "model": self.config.llm.model,
            "messages": messages,
            "temperature": self.config.llm.temperature,
            "max_tokens": self.config.llm.max_tokens,
        }

        response = await self.http_client.post(
            url,
            headers=headers,
            json=payload,
        )

        if response.status_code != 200:
            print(f"❌ Groq error: {response.status_code} - {response.text}")
            return ""

        result = response.json()

        try:
            return result["choices"][0]["message"]["content"]
        except (KeyError, IndexError):
            return ""

    async def _speak_response(self, text: str):
        """Generate TTS audio and send to client with barge-in support."""
        await self._speak_response_timed(text, None, None, None)

    async def _speak_response_timed(self, text: str, t_tts_start: float = None, timings: dict = None, t_total_start: float = None):
        """Generate TTS audio with timing metrics. Routes to appropriate provider."""
        provider = self.config.tts.provider.lower()

        # Route to ElevenLabs if configured and available
        if provider == "elevenlabs" and self.elevenlabs_client:
            await self._speak_response_elevenlabs(text, t_tts_start, timings, t_total_start)
        # Route to OpenAI streaming or blocking based on config
        elif self.config.tts.streaming:
            await self._speak_response_streaming(text, t_tts_start, timings, t_total_start)
        else:
            await self._speak_response_blocking(text, t_tts_start, timings, t_total_start)

    async def _speak_response_streaming(self, text: str, t_tts_start: float = None, timings: dict = None, t_total_start: float = None):
        """
        STREAMING TTS - Send audio chunks as they're generated.

        This dramatically reduces Time To First Byte (TTFB) because we start
        sending audio immediately instead of waiting for full generation.

        Expected improvement: TTFB from ~2800ms to ~1500ms
        """
        try:
            self.is_speaking_tts = True
            self.barge_in_detected = False

            t_gen_start = time.time()
            t_first_chunk = None
            bytes_sent = 0
            total_bytes = 0

            print(f"🔊 [STREAMING] Generating TTS with OpenAI...", flush=True)

            # Use streaming response - iter_bytes() yields chunks as they arrive
            async with self.openai_client.audio.speech.with_streaming_response.create(
                model="tts-1",
                voice=self.config.tts.voice,
                input=text,
                response_format="pcm",
            ) as response:
                # Stream chunks directly to WebSocket as they arrive
                async for chunk in response.iter_bytes(chunk_size=4096):
                    # Track time to first byte
                    if t_first_chunk is None:
                        t_first_chunk = time.time()
                        ttfb_ms = (t_first_chunk - t_gen_start) * 1000
                        print(f"⚡ [STREAMING] First chunk: {ttfb_ms:.0f}ms TTFB", flush=True)

                    # Check for barge-in
                    if self.barge_in_detected:
                        print(f"🛑 Barge-in detected! Stopping TTS at {bytes_sent} bytes", flush=True)
                        break

                    # Send chunk to client
                    try:
                        await self.websocket.send_bytes(chunk)
                        bytes_sent += len(chunk)
                        total_bytes += len(chunk)
                        # Small yield to allow barge-in detection
                        await asyncio.sleep(0.005)  # 5ms
                    except Exception as e:
                        print(f"⚠️ Failed to send audio chunk: {e}", flush=True)
                        break

            t_end = time.time()
            tts_total_ms = (t_end - t_gen_start) * 1000
            ttfb_ms = (t_first_chunk - t_gen_start) * 1000 if t_first_chunk else tts_total_ms

            # Log timing
            if timings is not None:
                timings['tts_gen'] = ttfb_ms  # TTFB is what matters for streaming
                timings['tts_send'] = tts_total_ms - ttfb_ms
                timings['tts_total'] = tts_total_ms
                timings['streaming'] = True

            print(f"✅ [STREAMING] Sent {bytes_sent} bytes, TTFB={ttfb_ms:.0f}ms, total={tts_total_ms:.0f}ms", flush=True)

            # ====== LATENCY SUMMARY ======
            if timings is not None and t_total_start is not None:
                total_ms = (time.time() - t_total_start) * 1000
                actual_ttfb = timings.get('stt', 0) + timings.get('pause', 0) + timings.get('llm', 0) + ttfb_ms
                print(f"\n{'='*50}", flush=True)
                print(f"📊 LATENCY BREAKDOWN [STREAMING] (Turn #{len(self.turns)//2})", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   STT (Deepgram):    {timings.get('stt', 0):>6.0f}ms", flush=True)
                print(f"   Thinking pause:    {timings.get('pause', 0):>6.0f}ms", flush=True)
                print(f"   LLM (Groq):        {timings.get('llm', 0):>6.0f}ms", flush=True)
                print(f"   TTS TTFB:          {ttfb_ms:>6.0f}ms ⚡", flush=True)
                print(f"   TTS streaming:     {timings.get('tts_send', 0):>6.0f}ms", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   TOTAL:             {total_ms:>6.0f}ms", flush=True)
                print(f"   TIME TO FIRST BYTE:{actual_ttfb:>6.0f}ms ⚡", flush=True)
                print(f"{'='*50}\n", flush=True)

        except Exception as e:
            print(f"❌ [STREAMING] TTS error: {e}", flush=True)
            # Fallback to blocking mode
            print(f"🔄 Falling back to blocking TTS...", flush=True)
            await self._speak_response_blocking(text, t_tts_start, timings, t_total_start)
        finally:
            self.is_speaking_tts = False

    async def _speak_response_blocking(self, text: str, t_tts_start: float = None, timings: dict = None, t_total_start: float = None):
        """
        BLOCKING TTS - Wait for full audio before sending (original behavior).

        Use this as fallback or when streaming causes issues.
        Set TTS_STREAMING=false to use this mode.
        """
        try:
            self.is_speaking_tts = True  # Mark AI as speaking
            self.barge_in_detected = False

            # Track TTS generation time
            t_gen_start = time.time()

            print(f"🔊 [BLOCKING] Generating TTS with OpenAI...", flush=True)

            # Generate speech with OpenAI TTS (waits for full response)
            response = await self.openai_client.audio.speech.create(
                model="tts-1",
                voice=self.config.tts.voice,
                input=text,
                response_format="pcm",  # Raw PCM16
            )

            t_gen_end = time.time()
            tts_gen_ms = (t_gen_end - t_gen_start) * 1000

            # Get the full audio content
            audio_content = response.content
            audio_duration_ms = len(audio_content) / 32  # 32 bytes per ms at 16kHz, 16-bit

            print(f"⏱️ [BLOCKING] TTS generation: {tts_gen_ms:.0f}ms ({len(audio_content)} bytes = {audio_duration_ms:.0f}ms audio)", flush=True)

            # Track time to first byte (TTFB)
            t_send_start = time.time()

            # Send audio in chunks to client with barge-in check
            chunk_size = 4096  # ~128ms at 16kHz
            bytes_sent = 0
            first_byte_sent = False
            for i in range(0, len(audio_content), chunk_size):
                # Check for barge-in (user started speaking)
                if self.barge_in_detected:
                    print(f"🛑 Barge-in detected! Stopping TTS at {bytes_sent} bytes", flush=True)
                    break

                chunk = audio_content[i:i+chunk_size]
                try:
                    await self.websocket.send_bytes(chunk)
                    bytes_sent += len(chunk)
                    if not first_byte_sent:
                        ttfb = (time.time() - t_send_start) * 1000
                        first_byte_sent = True
                    # Small delay to allow barge-in detection
                    await asyncio.sleep(0.01)  # 10ms
                except Exception as e:
                    print(f"⚠️ Failed to send audio chunk: {e}", flush=True)
                    break

            t_send_end = time.time()

            # Log TTS timing
            if timings is not None and t_tts_start is not None:
                timings['tts_gen'] = tts_gen_ms
                timings['tts_send'] = (t_send_end - t_send_start) * 1000
                timings['tts_total'] = (t_send_end - t_tts_start) * 1000
                timings['streaming'] = False

            print(f"✅ [BLOCKING] Sent TTS response: {bytes_sent} bytes ({len(text)} chars)", flush=True)

            # ====== LATENCY SUMMARY ======
            if timings is not None and t_total_start is not None:
                total_ms = (time.time() - t_total_start) * 1000
                print(f"\n{'='*50}", flush=True)
                print(f"📊 LATENCY BREAKDOWN [BLOCKING] (Turn #{len(self.turns)//2})", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   STT (Deepgram):    {timings.get('stt', 0):>6.0f}ms", flush=True)
                print(f"   Thinking pause:    {timings.get('pause', 0):>6.0f}ms", flush=True)
                print(f"   LLM (Groq):        {timings.get('llm', 0):>6.0f}ms", flush=True)
                print(f"   TTS gen (OpenAI):  {timings.get('tts_gen', 0):>6.0f}ms", flush=True)
                print(f"   TTS send:          {timings.get('tts_send', 0):>6.0f}ms", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   TOTAL:             {total_ms:>6.0f}ms", flush=True)
                print(f"   Time to first byte:{timings.get('stt', 0) + timings.get('pause', 0) + timings.get('llm', 0) + timings.get('tts_gen', 0):>6.0f}ms", flush=True)
                print(f"{'='*50}\n", flush=True)

        except Exception as e:
            print(f"❌ [BLOCKING] TTS error: {e}", flush=True)
        finally:
            self.is_speaking_tts = False  # AI done speaking

    async def _speak_response_elevenlabs(self, text: str, t_tts_start: float = None, timings: dict = None, t_total_start: float = None):
        """
        ELEVENLABS STREAMING TTS - True streaming with low TTFB.

        ElevenLabs generates and streams audio simultaneously, giving us
        much lower Time To First Byte compared to OpenAI.

        Expected TTFB: ~200-400ms (vs ~1100ms for OpenAI)
        """
        try:
            self.is_speaking_tts = True
            self.barge_in_detected = False

            t_gen_start = time.time()
            t_first_chunk = None
            bytes_sent = 0

            voice_id = self.config.tts.elevenlabs_voice_id
            print(f"🔊 [ELEVENLABS] Generating TTS (voice: {voice_id})...", flush=True)

            # Use ElevenLabs streaming API - generates and streams simultaneously
            # pcm_24000 format = 16-bit PCM at 24kHz (matches OpenAI TTS output)
            # Note: stream() returns an async generator, don't await it
            audio_stream = self.elevenlabs_client.text_to_speech.stream(
                voice_id=voice_id,
                text=text,
                model_id="eleven_turbo_v2_5",  # Fastest model
                output_format="pcm_24000",  # 24kHz PCM16 to match OpenAI TTS
            )

            # Stream chunks directly to WebSocket as they arrive
            async for chunk in audio_stream:
                # Track time to first byte
                if t_first_chunk is None:
                    t_first_chunk = time.time()
                    ttfb_ms = (t_first_chunk - t_gen_start) * 1000
                    print(f"⚡ [ELEVENLABS] First chunk: {ttfb_ms:.0f}ms TTFB", flush=True)

                # Check for barge-in
                if self.barge_in_detected:
                    print(f"🛑 Barge-in detected! Stopping TTS at {bytes_sent} bytes", flush=True)
                    break

                # Send chunk to client
                try:
                    await self.websocket.send_bytes(chunk)
                    bytes_sent += len(chunk)
                    # Small yield to allow barge-in detection
                    await asyncio.sleep(0.005)  # 5ms
                except Exception as e:
                    print(f"⚠️ Failed to send audio chunk: {e}", flush=True)
                    break

            t_end = time.time()
            tts_total_ms = (t_end - t_gen_start) * 1000
            ttfb_ms = (t_first_chunk - t_gen_start) * 1000 if t_first_chunk else tts_total_ms

            # Log timing
            if timings is not None:
                timings['tts_gen'] = ttfb_ms
                timings['tts_send'] = tts_total_ms - ttfb_ms
                timings['tts_total'] = tts_total_ms
                timings['tts_provider'] = 'elevenlabs'

            print(f"✅ [ELEVENLABS] Sent {bytes_sent} bytes, TTFB={ttfb_ms:.0f}ms, total={tts_total_ms:.0f}ms", flush=True)

            # ====== LATENCY SUMMARY ======
            if timings is not None and t_total_start is not None:
                total_ms = (time.time() - t_total_start) * 1000
                actual_ttfb = timings.get('stt', 0) + timings.get('pause', 0) + timings.get('llm', 0) + ttfb_ms
                print(f"\n{'='*50}", flush=True)
                print(f"📊 LATENCY BREAKDOWN [ELEVENLABS] (Turn #{len(self.turns)//2})", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   STT (Deepgram):    {timings.get('stt', 0):>6.0f}ms", flush=True)
                print(f"   Thinking pause:    {timings.get('pause', 0):>6.0f}ms", flush=True)
                print(f"   LLM (Groq):        {timings.get('llm', 0):>6.0f}ms", flush=True)
                print(f"   TTS TTFB:          {ttfb_ms:>6.0f}ms ⚡ ElevenLabs", flush=True)
                print(f"   TTS streaming:     {timings.get('tts_send', 0):>6.0f}ms", flush=True)
                print(f"{'='*50}", flush=True)
                print(f"   TOTAL:             {total_ms:>6.0f}ms", flush=True)
                print(f"   TIME TO FIRST BYTE:{actual_ttfb:>6.0f}ms ⚡", flush=True)
                print(f"{'='*50}\n", flush=True)

        except Exception as e:
            print(f"❌ [ELEVENLABS] TTS error: {e}", flush=True)
            # Fallback to OpenAI
            print(f"🔄 Falling back to OpenAI TTS...", flush=True)
            await self._speak_response_streaming(text, t_tts_start, timings, t_total_start)
        finally:
            self.is_speaking_tts = False

    async def _process_control(self, message: str):
        """Process JSON control messages from client."""
        import json
        try:
            msg = json.loads(message)
            msg_type = msg.get("type", "")

            if msg_type == "ping":
                # Respond to keepalive
                await self.websocket.send_text('{"type": "pong"}')

            elif msg_type == "stop":
                # Client wants to end session
                print(f"🛑 Stop requested by client")
                self.is_running = False

            else:
                print(f"⚠️ Unknown control message: {msg_type}")

        except json.JSONDecodeError:
            print(f"⚠️ Invalid JSON: {message[:50]}...")

    async def cleanup(self):
        """Clean up resources and finalize session."""
        self.is_running = False

        duration = time.time() - self.start_time if self.start_time else 0

        print(f"🔚 Session cleanup: {self.session_id[:8]}")
        print(f"   Duration: {duration:.1f}s")
        print(f"   Chunks: {self.chunks_received}")
        print(f"   Bytes: {self.bytes_received / 1024:.1f}KB")
        print(f"   Turns: {len(self.turns)}")

        # Store conversation in Firestore
        if self.turns:
            try:
                transcript = self._build_transcript()
                await self.firestore_client.store_voice_conversation(
                    uid=self.uid,
                    session_id=self.session_id,
                    transcript=transcript,
                    segments=[{
                        "role": t.role,
                        "text": t.text,
                        "timestamp": t.timestamp
                    } for t in self.turns],
                    duration_seconds=duration,
                    source="voice_mode_v2_manual",
                )
                print(f"💾 Conversation saved")

                # Call n8n agents
                asyncio.create_task(self._call_agents(transcript))

            except Exception as e:
                print(f"❌ Failed to save conversation: {e}")

        # Close HTTP client
        if self.http_client:
            await self.http_client.aclose()

    def _build_transcript(self) -> str:
        """Build transcript string from turns."""
        lines = []
        for turn in self.turns:
            role = "User" if turn.role == "user" else "Assistant"
            lines.append(f"{role}: {turn.text}")
        return "\n".join(lines)

    async def _call_agents(self, transcript: str):
        """Call n8n memory and summary agents."""
        try:
            await self.n8n_client.call_memory_agent(
                uid=self.uid,
                conversation_id=self.session_id,
                transcript=transcript,
                segments=[{
                    "role": t.role,
                    "text": t.text,
                    "timestamp": t.timestamp
                } for t in self.turns],
            )

            await self.n8n_client.call_summary_agent(
                uid=self.uid,
                conversation_id=self.session_id,
                transcript=transcript,
                segments=[{
                    "role": t.role,
                    "text": t.text,
                    "timestamp": t.timestamp
                } for t in self.turns],
            )

            print(f"✅ n8n agents called for {self.session_id[:8]}")

        except Exception as e:
            print(f"⚠️ n8n agent call failed: {e}")

    def _build_system_prompt(self, ella_config: dict) -> str:
        """Build system prompt with persona and memory blocks."""
        persona = ella_config.get("persona", "You are Ella, a warm and caring AI companion.")
        blocks = ella_config.get("blocks", {})
        user_info = ella_config.get("user", {})

        user_name = user_info.get("name", "the user")
        user_profile = blocks.get("user_profile", "No profile available.")
        rolling_memories = blocks.get("rolling_memories", "No recent memories.")
        rolling_summaries = blocks.get("rolling_summaries", "No recent conversations.")

        return f"""
{persona}

## About {user_name}
{user_profile}

## Recent Memories
{rolling_memories}

## Recent Conversations
{rolling_summaries}

## Voice Conversation Guidelines (CRITICAL)
- MAXIMUM 2-3 sentences per response (under 30 words ideal)
- This is a VOICE conversation - long responses feel like lectures
- Be conversational, not comprehensive
- One idea per turn, then wait for user
- Ask short follow-up questions to maintain dialogue
- Never list multiple points - pick the most important one
- Respond like you're chatting, not writing an essay
""".strip()

    def _default_system_prompt(self) -> str:
        """Default system prompt when Ella config unavailable."""
        return """
You are Ella, a warm and caring AI companion.

## Voice Conversation Guidelines (CRITICAL)
- MAXIMUM 2-3 sentences per response (under 30 words ideal)
- This is a VOICE conversation - long responses feel like lectures
- Be conversational, not comprehensive
- One idea per turn, then wait for user
- Ask short follow-up questions to maintain dialogue
- Never list multiple points - pick the most important one
- Respond like you're chatting, not writing an essay
""".strip()


async def run_manual_voice_session(
    websocket: WebSocket,
    uid: str,
    session_id: Optional[str] = None,
    config: Optional[PipelineConfig] = None,
):
    """
    Run a voice session using the manual pipeline.

    This is the entry point called from the router.
    """
    session_id = session_id or str(uuid.uuid4())

    pipeline = ManualVoicePipeline(
        websocket=websocket,
        uid=uid,
        session_id=session_id,
        config=config or DEFAULT_CONFIG,
    )

    try:
        await pipeline.initialize()
        await pipeline.run()
    except Exception as e:
        print(f"❌ Manual pipeline error: {e}")
        raise
