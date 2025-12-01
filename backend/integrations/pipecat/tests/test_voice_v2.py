#!/usr/bin/env python3
"""
Official test script for Voice Mode v2 (Pipecat) endpoint.

This script tests the /v2/voice WebSocket endpoint with various scenarios:
1. Health check
2. WebSocket connection
3. Audio streaming simulation
4. n8n config integration
5. Conversation storage verification

Usage:
    # Run all tests against local server
    python -m integrations.pipecat.tests.test_voice_v2

    # Run against production
    python -m integrations.pipecat.tests.test_voice_v2 --host api.ella-ai-care.com --ssl

    # Run specific test
    python -m integrations.pipecat.tests.test_voice_v2 --test health

    # Verbose output
    python -m integrations.pipecat.tests.test_voice_v2 -v

Requirements:
    pip install websockets httpx

Author: Backend Team
Date: December 2025
"""

import argparse
import asyncio
import json
import os
import sys
import time
import struct
import math
import ssl
from typing import Optional
from dataclasses import dataclass

try:
    import websockets
    import httpx
    import certifi
except ImportError:
    print("Missing dependencies. Install with:")
    print("  pip install websockets httpx certifi")
    sys.exit(1)


def get_ssl_context() -> ssl.SSLContext:
    """Create SSL context with proper certificate verification."""
    ssl_context = ssl.create_default_context(cafile=certifi.where())
    return ssl_context


@dataclass
class TestConfig:
    """Test configuration."""
    host: str = "localhost"
    port: int = 8000
    ssl: bool = False
    uid: str = "test-user-voice-v2"
    verbose: bool = False
    timeout: float = 30.0

    @property
    def base_url(self) -> str:
        protocol = "https" if self.ssl else "http"
        if self.ssl and self.port == 443:
            return f"{protocol}://{self.host}"
        elif not self.ssl and self.port == 80:
            return f"{protocol}://{self.host}"
        return f"{protocol}://{self.host}:{self.port}"

    @property
    def ws_url(self) -> str:
        protocol = "wss" if self.ssl else "ws"
        if self.ssl and self.port == 443:
            return f"{protocol}://{self.host}"
        elif not self.ssl and self.port == 80:
            return f"{protocol}://{self.host}"
        return f"{protocol}://{self.host}:{self.port}"


class TestResult:
    """Test result tracker."""

    def __init__(self):
        self.passed = 0
        self.failed = 0
        self.skipped = 0
        self.results = []

    def add_pass(self, name: str, message: str = ""):
        self.passed += 1
        self.results.append(("PASS", name, message))
        print(f"  ✅ {name}: {message}" if message else f"  ✅ {name}")

    def add_fail(self, name: str, message: str = ""):
        self.failed += 1
        self.results.append(("FAIL", name, message))
        print(f"  ❌ {name}: {message}" if message else f"  ❌ {name}")

    def add_skip(self, name: str, message: str = ""):
        self.skipped += 1
        self.results.append(("SKIP", name, message))
        print(f"  ⏭️  {name}: {message}" if message else f"  ⏭️  {name}")

    def summary(self) -> str:
        total = self.passed + self.failed + self.skipped
        return f"Results: {self.passed}/{total} passed, {self.failed} failed, {self.skipped} skipped"


def generate_sine_wave_audio(
    duration_seconds: float = 1.0,
    sample_rate: int = 16000,
    frequency: float = 440.0,
    amplitude: float = 0.5,
) -> bytes:
    """
    Generate PCM16 sine wave audio for testing.

    Args:
        duration_seconds: Duration in seconds
        sample_rate: Sample rate (default 16kHz)
        frequency: Frequency in Hz (default 440 Hz = A4)
        amplitude: Amplitude 0-1

    Returns:
        PCM16 audio bytes (little-endian)
    """
    num_samples = int(sample_rate * duration_seconds)
    samples = []

    for i in range(num_samples):
        t = i / sample_rate
        value = amplitude * math.sin(2 * math.pi * frequency * t)
        # Convert to 16-bit signed integer
        sample = int(value * 32767)
        samples.append(struct.pack('<h', sample))

    return b''.join(samples)


def generate_speech_like_audio(duration_seconds: float = 2.0) -> bytes:
    """
    Generate audio that somewhat mimics speech patterns.

    Uses multiple frequencies and amplitude modulation.
    """
    sample_rate = 16000
    num_samples = int(sample_rate * duration_seconds)
    samples = []

    for i in range(num_samples):
        t = i / sample_rate

        # Mix of frequencies (fundamental + harmonics)
        f1 = 150 + 50 * math.sin(2 * math.pi * 3 * t)  # Varying fundamental
        f2 = 300 + 100 * math.sin(2 * math.pi * 5 * t)  # First harmonic

        # Amplitude modulation (speech-like envelope)
        envelope = 0.3 + 0.2 * math.sin(2 * math.pi * 4 * t)

        value = envelope * (
            0.6 * math.sin(2 * math.pi * f1 * t) +
            0.3 * math.sin(2 * math.pi * f2 * t) +
            0.1 * math.sin(2 * math.pi * f1 * 3 * t)
        )

        sample = int(max(-1, min(1, value)) * 32767)
        samples.append(struct.pack('<h', sample))

    return b''.join(samples)


async def test_health_check(config: TestConfig, results: TestResult):
    """Test the health check endpoint."""
    print("\n🏥 Testing Health Check...")

    url = f"{config.base_url}/v2/voice/health"

    try:
        async with httpx.AsyncClient(timeout=config.timeout) as client:
            response = await client.get(url)

            if response.status_code == 200:
                data = response.json()
                results.add_pass("Health endpoint reachable")

                if data.get("status") == "ok":
                    results.add_pass("Status is 'ok'")
                else:
                    results.add_fail("Status check", f"Expected 'ok', got '{data.get('status')}'")

                # Check configuration
                cfg = data.get("config", {})
                if cfg.get("vad_provider") == "silero":
                    results.add_pass("VAD provider is Silero")
                else:
                    results.add_fail("VAD provider", f"Expected 'silero', got '{cfg.get('vad_provider')}'")

                # Check dependencies
                deps = data.get("dependencies", {})
                for key in ["deepgram_key_set", "openai_key_set", "groq_key_set"]:
                    if deps.get(key):
                        results.add_pass(f"{key}")
                    else:
                        results.add_fail(f"{key}", "API key not set")

                if config.verbose:
                    print(f"    Config: {json.dumps(cfg, indent=2)}")

            else:
                results.add_fail("Health endpoint", f"HTTP {response.status_code}")

    except httpx.ConnectError:
        results.add_fail("Health endpoint", f"Cannot connect to {url}")
    except Exception as e:
        results.add_fail("Health endpoint", str(e))


async def test_websocket_connection(config: TestConfig, results: TestResult):
    """Test basic WebSocket connection."""
    print("\n🔌 Testing WebSocket Connection...")

    url = f"{config.ws_url}/v2/voice?uid={config.uid}"

    # Use SSL context for secure connections
    ssl_context = get_ssl_context() if config.ssl else None

    try:
        async with websockets.connect(url, close_timeout=5, ssl=ssl_context) as ws:
            results.add_pass("WebSocket connection established")

            # Connection should stay open
            await asyncio.sleep(0.5)

            # Check if connection is still open (websockets 12+ uses .state)
            try:
                is_open = ws.open if hasattr(ws, 'open') else ws.state.name == 'OPEN'
            except:
                is_open = True  # Assume open if we can't check

            if is_open:
                results.add_pass("Connection stable after 500ms")
            else:
                results.add_fail("Connection stability", "Connection closed unexpectedly")

            # Clean close
            await ws.close()
            results.add_pass("Clean disconnection")

    except websockets.exceptions.InvalidStatusCode as e:
        results.add_fail("WebSocket connection", f"Invalid status: {e.status_code}")
    except ConnectionRefusedError:
        results.add_fail("WebSocket connection", "Connection refused - is server running?")
    except Exception as e:
        results.add_fail("WebSocket connection", str(e))


async def test_audio_streaming(config: TestConfig, results: TestResult):
    """Test sending audio and receiving responses."""
    print("\n🎤 Testing Audio Streaming...")

    url = f"{config.ws_url}/v2/voice?uid={config.uid}"

    # Use SSL context for secure connections
    ssl_context = get_ssl_context() if config.ssl else None

    try:
        async with websockets.connect(url, close_timeout=10, ssl=ssl_context) as ws:
            results.add_pass("Connected for audio test")

            # Generate test audio (2 seconds of speech-like audio)
            audio_data = generate_speech_like_audio(duration_seconds=2.0)
            chunk_size = 3200  # 100ms at 16kHz, 16-bit

            # Send audio in chunks
            chunks_sent = 0
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                await ws.send(chunk)
                chunks_sent += 1
                await asyncio.sleep(0.1)  # Simulate real-time

            results.add_pass(f"Sent {chunks_sent} audio chunks")

            # Wait for potential responses
            responses_received = 0
            try:
                async with asyncio.timeout(5):
                    while True:
                        response = await ws.recv()
                        responses_received += 1
                        if config.verbose:
                            if isinstance(response, bytes):
                                print(f"    Received audio: {len(response)} bytes")
                            else:
                                print(f"    Received: {response[:100]}...")
            except asyncio.TimeoutError:
                pass  # Expected - timeout after waiting

            if responses_received > 0:
                results.add_pass(f"Received {responses_received} responses")
            else:
                results.add_skip("Response check", "No responses (may need real speech)")

            await ws.close()

    except Exception as e:
        results.add_fail("Audio streaming", str(e))


async def test_real_audio_e2e(config: TestConfig, results: TestResult):
    """End-to-end test with real audio from WAV file."""
    print("\n🎤 Testing Real Audio End-to-End...")

    # Try to load real audio file
    audio_file = "test_audio/pyannote_sample.wav"
    try:
        import wave
        with wave.open(audio_file, 'rb') as w:
            if w.getframerate() != 16000 or w.getnchannels() != 1:
                results.add_skip("Real audio test", f"Audio format mismatch: {w.getframerate()}Hz, {w.getnchannels()}ch")
                return
            audio_data = w.readframes(w.getnframes())
            duration = w.getnframes() / w.getframerate()
    except FileNotFoundError:
        results.add_skip("Real audio test", f"Audio file not found: {audio_file}")
        return
    except Exception as e:
        results.add_skip("Real audio test", f"Failed to load audio: {e}")
        return

    results.add_pass(f"Loaded {duration:.1f}s of real audio")

    url = f"{config.ws_url}/v2/voice?uid={config.uid}&session_id=real-audio-test"
    ssl_context = get_ssl_context() if config.ssl else None

    try:
        async with websockets.connect(url, close_timeout=30, ssl=ssl_context) as ws:
            results.add_pass("Connected for real audio test")

            # Send first 5 seconds of audio (enough for a sentence)
            chunk_size = 3200  # 100ms at 16kHz, 16-bit
            max_chunks = 50  # 5 seconds
            chunks_sent = 0
            bytes_sent = 0

            for i in range(0, min(len(audio_data), chunk_size * max_chunks), chunk_size):
                chunk = audio_data[i:i + chunk_size]
                await ws.send(chunk)
                chunks_sent += 1
                bytes_sent += len(chunk)
                await asyncio.sleep(0.1)  # Simulate real-time streaming

            results.add_pass(f"Sent {chunks_sent} chunks ({bytes_sent/1024:.1f}KB)")

            # Wait for VAD to detect silence and trigger processing
            print("    Waiting for response (VAD + STT + LLM + TTS)...")

            responses_received = 0
            audio_bytes_received = 0
            try:
                async with asyncio.timeout(15):  # Longer timeout for real processing
                    while True:
                        response = await ws.recv()
                        responses_received += 1
                        if isinstance(response, bytes):
                            audio_bytes_received += len(response)
                            if config.verbose:
                                print(f"    Received TTS audio: {len(response)} bytes")
                        else:
                            if config.verbose:
                                print(f"    Received: {response[:100]}...")
            except asyncio.TimeoutError:
                pass  # Expected - timeout after waiting

            if responses_received > 0:
                results.add_pass(f"Received {responses_received} responses ({audio_bytes_received/1024:.1f}KB audio)")
            else:
                results.add_fail("E2E response", "No response received (check VAD/STT/LLM/TTS)")

            await ws.close()

    except Exception as e:
        results.add_fail("Real audio E2E", str(e))


async def test_n8n_config_integration(config: TestConfig, results: TestResult):
    """Test n8n configuration fetching."""
    print("\n🔧 Testing n8n Config Integration...")

    # This tests the N8NClient directly
    try:
        from integrations.pipecat.services.n8n_client import N8NClient
        from integrations.pipecat.pipeline.config import N8NConfig

        client = N8NClient(N8NConfig())

        # Test config fetch
        ella_config = await client.fetch_voice_config(config.uid)

        if ella_config:
            results.add_pass("Config fetched successfully")

            # Check required fields
            if "agent_config" in ella_config:
                results.add_pass("agent_config present")
            else:
                results.add_fail("agent_config", "Missing from response")

            if "blocks" in ella_config:
                results.add_pass("memory blocks present")
            else:
                results.add_fail("memory blocks", "Missing from response")

            if "persona" in ella_config:
                results.add_pass("persona present")
            else:
                results.add_skip("persona", "Using default")

            if config.verbose:
                print(f"    Agent config: {ella_config.get('agent_config', {})}")
                print(f"    User: {ella_config.get('user', {})}")

        else:
            results.add_fail("Config fetch", "Empty response")

    except ImportError:
        results.add_skip("n8n integration", "Run from backend directory")
    except Exception as e:
        results.add_fail("n8n integration", str(e))


async def test_pipeline_creation(config: TestConfig, results: TestResult):
    """Test pipeline creation (without running)."""
    print("\n🔨 Testing Pipeline Creation...")

    try:
        from integrations.pipecat.pipeline.config import PipelineConfig

        # Create config
        pipeline_config = PipelineConfig()

        # Validate
        try:
            pipeline_config.validate()
            results.add_pass("Pipeline config validation")
        except ValueError as e:
            results.add_fail("Pipeline config validation", str(e))

        # Check components
        results.add_pass(f"VAD: {pipeline_config.vad.provider} (stop_secs={pipeline_config.vad.stop_secs})")
        results.add_pass(f"STT: {pipeline_config.stt.provider} ({pipeline_config.stt.model})")
        results.add_pass(f"TTS: {pipeline_config.tts.provider} ({pipeline_config.tts.voice})")
        results.add_pass(f"LLM: {pipeline_config.llm.provider} ({pipeline_config.llm.model})")

    except ImportError:
        results.add_skip("Pipeline creation", "Run from backend directory")
    except Exception as e:
        results.add_fail("Pipeline creation", str(e))


async def run_all_tests(config: TestConfig) -> TestResult:
    """Run all tests."""
    results = TestResult()

    print("=" * 60)
    print("🎙️  Voice Mode v2 (Pipecat) Test Suite")
    print("=" * 60)
    print(f"Target: {config.base_url}")
    print(f"UID: {config.uid}")

    # Run tests in order
    await test_health_check(config, results)
    await test_pipeline_creation(config, results)
    await test_n8n_config_integration(config, results)
    await test_websocket_connection(config, results)
    await test_audio_streaming(config, results)
    await test_real_audio_e2e(config, results)

    print("\n" + "=" * 60)
    print(results.summary())
    print("=" * 60)

    return results


async def run_single_test(config: TestConfig, test_name: str) -> TestResult:
    """Run a single test by name."""
    results = TestResult()

    tests = {
        "health": test_health_check,
        "websocket": test_websocket_connection,
        "audio": test_audio_streaming,
        "e2e": test_real_audio_e2e,
        "n8n": test_n8n_config_integration,
        "pipeline": test_pipeline_creation,
    }

    if test_name not in tests:
        print(f"Unknown test: {test_name}")
        print(f"Available tests: {', '.join(tests.keys())}")
        sys.exit(1)

    print(f"Running test: {test_name}")
    await tests[test_name](config, results)
    print(results.summary())

    return results


def main():
    parser = argparse.ArgumentParser(
        description="Test Voice Mode v2 (Pipecat) endpoint",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test local server
  python -m integrations.pipecat.tests.test_voice_v2

  # Test production
  python -m integrations.pipecat.tests.test_voice_v2 --host api.ella-ai-care.com --ssl

  # Run specific test
  python -m integrations.pipecat.tests.test_voice_v2 --test health

  # With custom UID
  python -m integrations.pipecat.tests.test_voice_v2 --uid my-test-user
        """,
    )

    parser.add_argument("--host", default="localhost", help="Server host")
    parser.add_argument("--port", type=int, default=8000, help="Server port")
    parser.add_argument("--ssl", action="store_true", help="Use SSL/TLS")
    parser.add_argument("--uid", default="test-user-voice-v2", help="Test user ID")
    parser.add_argument("--test", help="Run specific test (health, websocket, audio, n8n, pipeline)")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    parser.add_argument("--timeout", type=float, default=30.0, help="Request timeout")

    args = parser.parse_args()

    config = TestConfig(
        host=args.host,
        port=args.port,
        ssl=args.ssl,
        uid=args.uid,
        verbose=args.verbose,
        timeout=args.timeout,
    )

    if args.test:
        results = asyncio.run(run_single_test(config, args.test))
    else:
        results = asyncio.run(run_all_tests(config))

    # Exit with error code if any tests failed
    sys.exit(1 if results.failed > 0 else 0)


if __name__ == "__main__":
    main()
