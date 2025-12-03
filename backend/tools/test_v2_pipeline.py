#!/usr/bin/env python3
"""
V2 Voice Pipeline Test Script

Tests each node in the voice pipeline independently.
Usage:
    python test_v2_pipeline.py --test echo      # Test WebSocket echo
    python test_v2_pipeline.py --test stt       # Test STT only
    python test_v2_pipeline.py --test llm       # Test LLM only
    python test_v2_pipeline.py --test tts       # Test TTS only
    python test_v2_pipeline.py --test full      # Test full pipeline
    python test_v2_pipeline.py --test all       # Run all tests
"""

import asyncio
import argparse
import json
import ssl
import struct
import time
import math
from pathlib import Path

try:
    import websockets
    import certifi
except ImportError:
    print("Installing required packages...")
    import subprocess
    subprocess.run(["pip", "install", "websockets", "certifi"])
    import websockets
    import certifi

BASE_URL = "wss://api.ella-ai-care.com"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())

# Test audio file path
AUDIO_FILE = Path("/tmp/test_speech.pcm")


def generate_speech_audio(text="Hello, this is a test", duration_sec=2.0):
    """Generate test audio using macOS say command"""
    import subprocess
    import tempfile

    aiff_file = tempfile.mktemp(suffix=".aiff")
    pcm_file = Path("/tmp/test_speech.pcm")

    # Generate speech
    subprocess.run(["say", "-o", aiff_file, text], check=True)

    # Convert to PCM16 16kHz mono
    subprocess.run([
        "ffmpeg", "-y", "-i", aiff_file,
        "-ar", "16000", "-ac", "1", "-f", "s16le",
        str(pcm_file)
    ], check=True, capture_output=True)

    return pcm_file


def generate_sine_wave(duration_sec=2.0, freq=440, sample_rate=16000):
    """Generate a sine wave for testing"""
    samples = int(sample_rate * duration_sec)
    audio = []
    for i in range(samples):
        t = i / sample_rate
        value = int(16000 * math.sin(2 * math.pi * freq * t))
        audio.append(struct.pack('<h', value))
    return b''.join(audio)


async def test_echo():
    """Test 1: WebSocket Echo - verify basic WebSocket send/receive"""
    print("\n" + "="*60)
    print("TEST 1: WebSocket Echo")
    print("="*60)

    uri = f"{BASE_URL}/v2/voice/echo?uid=test&session_id=echo_{int(time.time())}"
    print(f"Connecting to: {uri}")

    try:
        async with websockets.connect(uri, ssl=SSL_CONTEXT, close_timeout=5) as ws:
            print("✅ Connected")

            # Send test data
            test_data = b"ECHO_TEST_12345"
            t0 = time.time()
            await ws.send(test_data)
            print(f"📤 Sent: {len(test_data)} bytes")

            # Wait for echo
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=5.0)
                latency = (time.time() - t0) * 1000

                if response == test_data:
                    print(f"✅ PASS: Echo received in {latency:.1f}ms")
                    return True
                else:
                    print(f"❌ FAIL: Response mismatch")
                    print(f"   Sent: {test_data}")
                    print(f"   Got:  {response}")
                    return False
            except asyncio.TimeoutError:
                print("❌ FAIL: No echo received within 5s")
                return False

    except Exception as e:
        print(f"❌ FAIL: {e}")
        # This endpoint might not exist yet
        if "404" in str(e) or "not found" in str(e).lower():
            print("   (Endpoint not implemented yet - Backend TODO)")
        return False


async def test_stt():
    """Test 3: STT Only - send audio, get transcription text"""
    print("\n" + "="*60)
    print("TEST 3: STT Only")
    print("="*60)

    uri = f"{BASE_URL}/v2/voice/stt-only?uid=test&session_id=stt_{int(time.time())}"
    print(f"Connecting to: {uri}")

    # Get or generate audio
    if AUDIO_FILE.exists():
        audio_data = AUDIO_FILE.read_bytes()
        print(f"Using existing audio: {len(audio_data)} bytes")
    else:
        print("Generating test audio...")
        generate_speech_audio("Hello, this is a test of speech to text")
        audio_data = AUDIO_FILE.read_bytes()

    try:
        async with websockets.connect(uri, ssl=SSL_CONTEXT, close_timeout=10) as ws:
            print("✅ Connected")

            # Send audio at real-time pace
            chunk_size = 3200  # 100ms at 16kHz PCM16
            t0 = time.time()

            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                await ws.send(chunk)
                await asyncio.sleep(0.1)

            # Send silence for VAD
            silence = bytes(chunk_size)
            for _ in range(20):
                await ws.send(silence)
                await asyncio.sleep(0.1)

            send_time = time.time() - t0
            print(f"📤 Sent audio in {send_time:.1f}s")

            # Wait for transcription
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=10.0)
                total_time = time.time() - t0

                if isinstance(response, str):
                    data = json.loads(response)
                    print(f"✅ PASS: Transcription received in {total_time:.1f}s")
                    print(f"   Text: {data.get('text', 'N/A')}")
                    return True
                else:
                    print(f"❌ FAIL: Expected JSON text, got bytes")
                    return False

            except asyncio.TimeoutError:
                print("❌ FAIL: No transcription received within 10s")
                return False

    except Exception as e:
        print(f"❌ FAIL: {e}")
        if "404" in str(e) or "not found" in str(e).lower():
            print("   (Endpoint not implemented yet - Backend TODO)")
        return False


async def test_llm():
    """Test 4: LLM Only - send text, get response text"""
    print("\n" + "="*60)
    print("TEST 4: LLM Only")
    print("="*60)

    uri = f"{BASE_URL}/v2/voice/llm-only?uid=test&session_id=llm_{int(time.time())}"
    print(f"Connecting to: {uri}")

    try:
        async with websockets.connect(uri, ssl=SSL_CONTEXT, close_timeout=15) as ws:
            print("✅ Connected")

            # Send test prompt
            prompt = "Hello, how are you today?"
            t0 = time.time()
            await ws.send(prompt)
            print(f"📤 Sent: '{prompt}'")

            # Wait for response
            try:
                response = await asyncio.wait_for(ws.recv(), timeout=15.0)
                latency = time.time() - t0

                if isinstance(response, str):
                    data = json.loads(response)
                    print(f"✅ PASS: LLM response in {latency:.1f}s")
                    print(f"   Response: {data.get('text', response)[:100]}...")
                    return True
                else:
                    print(f"❌ FAIL: Expected JSON text, got bytes")
                    return False

            except asyncio.TimeoutError:
                print("❌ FAIL: No LLM response within 15s")
                return False

    except Exception as e:
        print(f"❌ FAIL: {e}")
        if "404" in str(e) or "not found" in str(e).lower():
            print("   (Endpoint not implemented yet - Backend TODO)")
        return False


async def test_tts():
    """Test 5: TTS Only - send text, get audio"""
    print("\n" + "="*60)
    print("TEST 5: TTS Only")
    print("="*60)

    uri = f"{BASE_URL}/v2/voice/tts-only?uid=test&session_id=tts_{int(time.time())}"
    print(f"Connecting to: {uri}")

    try:
        async with websockets.connect(uri, ssl=SSL_CONTEXT, close_timeout=15) as ws:
            print("✅ Connected")

            # Send text for TTS
            text = "Hello, this is a test of text to speech."
            t0 = time.time()
            await ws.send(text)
            print(f"📤 Sent: '{text}'")

            # Collect audio chunks
            total_bytes = 0
            chunks = 0

            while True:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=10.0)

                    if isinstance(response, bytes):
                        total_bytes += len(response)
                        chunks += 1
                        print(f"   📥 Chunk {chunks}: +{len(response)} bytes")
                    elif isinstance(response, str):
                        data = json.loads(response)
                        if data.get("type") == "tts_complete":
                            break

                except asyncio.TimeoutError:
                    break

            latency = time.time() - t0

            if total_bytes > 0:
                # Calculate audio duration (24kHz PCM16 = 48000 bytes/sec)
                duration = total_bytes / 48000
                print(f"✅ PASS: TTS received in {latency:.1f}s")
                print(f"   Audio: {total_bytes} bytes ({duration:.1f}s), {chunks} chunks")
                return True
            else:
                print("❌ FAIL: No TTS audio received")
                return False

    except Exception as e:
        print(f"❌ FAIL: {e}")
        if "404" in str(e) or "not found" in str(e).lower():
            print("   (Endpoint not implemented yet - Backend TODO)")
        return False


async def test_full():
    """Test 6: Full Pipeline - audio in, audio out"""
    print("\n" + "="*60)
    print("TEST 6: Full Pipeline (Current /v2/voice)")
    print("="*60)

    session_id = f"full_{int(time.time())}"
    uri = f"{BASE_URL}/v2/voice?uid=test123&session_id={session_id}"
    print(f"Connecting to: {uri}")
    print(f"Session ID: {session_id} (check server logs)")

    # Get or generate audio
    if AUDIO_FILE.exists():
        audio_data = AUDIO_FILE.read_bytes()
        print(f"Using existing audio: {len(audio_data)} bytes")
    else:
        print("Generating test audio...")
        generate_speech_audio("Hello, this is a test of voice mode")
        audio_data = AUDIO_FILE.read_bytes()

    try:
        async with websockets.connect(uri, ssl=SSL_CONTEXT,
                                       ping_interval=20, ping_timeout=10,
                                       close_timeout=30) as ws:
            print("✅ Connected")

            # Send audio at real-time pace
            chunk_size = 3200  # 100ms at 16kHz PCM16
            t0 = time.time()
            chunks_sent = 0

            print("📤 Sending audio at real-time pace...")
            for i in range(0, len(audio_data), chunk_size):
                chunk = audio_data[i:i+chunk_size]
                await ws.send(chunk)
                chunks_sent += 1
                await asyncio.sleep(0.1)

            # Send 3 seconds of silence for VAD
            print("📤 Sending 3s silence for VAD...")
            silence = bytes(chunk_size)
            for _ in range(30):
                await ws.send(silence)
                await asyncio.sleep(0.1)

            send_time = time.time() - t0
            print(f"📤 Total: {chunks_sent} chunks + 3s silence in {send_time:.1f}s")

            # Wait for response
            print("⏳ Waiting for TTS response (20s timeout)...")
            total_bytes = 0
            chunks_received = 0
            messages = []

            response_start = None
            while True:
                try:
                    response = await asyncio.wait_for(ws.recv(), timeout=2.0)

                    if response_start is None:
                        response_start = time.time()
                        first_response_time = response_start - t0
                        print(f"⚡ First response at {first_response_time:.1f}s")

                    if isinstance(response, bytes):
                        total_bytes += len(response)
                        chunks_received += 1
                        print(f"   📥 Audio chunk {chunks_received}: +{len(response)} bytes")
                    else:
                        messages.append(response)
                        print(f"   📨 Message: {response[:100]}")

                except asyncio.TimeoutError:
                    if total_bytes > 0 or messages:
                        break  # Got some response, done waiting
                    if time.time() - t0 > 20:
                        break  # Overall timeout
                    print(".", end="", flush=True)

            total_time = time.time() - t0
            print()

            if total_bytes > 0:
                duration = total_bytes / 48000  # 24kHz PCM16
                print(f"✅ PASS: Full pipeline completed in {total_time:.1f}s")
                print(f"   Audio: {total_bytes} bytes ({duration:.1f}s), {chunks_received} chunks")
                print(f"   Messages: {len(messages)}")
                return True
            elif messages:
                print(f"⚠️ PARTIAL: Got messages but no audio")
                print(f"   Messages: {messages}")
                return False
            else:
                print(f"❌ FAIL: No response received")
                print(f"   Session: {session_id}")
                print(f"   Check server logs for this session!")
                return False

    except Exception as e:
        print(f"❌ FAIL: {e}")
        return False


async def run_all_tests():
    """Run all tests"""
    print("\n" + "="*60)
    print("V2 VOICE PIPELINE TEST SUITE")
    print("="*60)
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Server: {BASE_URL}")

    results = {}

    # Test 1: Echo (basic WebSocket)
    results['echo'] = await test_echo()

    # Test 3: STT only
    results['stt'] = await test_stt()

    # Test 4: LLM only
    results['llm'] = await test_llm()

    # Test 5: TTS only
    results['tts'] = await test_tts()

    # Test 6: Full pipeline
    results['full'] = await test_full()

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)
    for test_name, passed in results.items():
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {test_name}: {status}")

    total = len(results)
    passed = sum(1 for v in results.values() if v)
    print(f"\nTotal: {passed}/{total} tests passed")

    return all(results.values())


def main():
    parser = argparse.ArgumentParser(description="V2 Voice Pipeline Test Script")
    parser.add_argument("--test", choices=["echo", "stt", "llm", "tts", "full", "all"],
                       default="full", help="Which test to run")
    parser.add_argument("--generate-audio", action="store_true",
                       help="Generate fresh test audio")
    args = parser.parse_args()

    # Generate audio if requested or missing
    if args.generate_audio or not AUDIO_FILE.exists():
        print("Generating test audio...")
        try:
            generate_speech_audio("Hello, this is a test of voice mode")
            print(f"✅ Audio saved to {AUDIO_FILE}")
        except Exception as e:
            print(f"⚠️ Could not generate audio: {e}")
            print("   Using sine wave instead...")
            AUDIO_FILE.write_bytes(generate_sine_wave(2.0))

    # Run tests
    if args.test == "all":
        success = asyncio.run(run_all_tests())
    elif args.test == "echo":
        success = asyncio.run(test_echo())
    elif args.test == "stt":
        success = asyncio.run(test_stt())
    elif args.test == "llm":
        success = asyncio.run(test_llm())
    elif args.test == "tts":
        success = asyncio.run(test_tts())
    elif args.test == "full":
        success = asyncio.run(test_full())

    return 0 if success else 1


if __name__ == "__main__":
    exit(main())
