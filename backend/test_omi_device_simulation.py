#!/usr/bin/env python3
"""
Test script to simulate OMI device sending audio data to the backend.

This script:
1. Generates synthetic audio or uses a sample audio file
2. Encodes it as Opus frames (matching OMI device protocol)
3. Connects to the WebSocket endpoint /v4/listen
4. Sends audio data in real-time
5. Receives and displays transcription results

Usage:
    python test_omi_device_simulation.py [--audio-file path/to/audio.wav]
"""

import asyncio
import json
import os
import struct
import sys
import wave
from typing import Optional

# Set Opus library path for macOS Homebrew BEFORE importing opuslib
opus_lib_path = '/opt/homebrew/opt/opus/lib'
if os.path.exists(opus_lib_path):
    current_dyld = os.environ.get('DYLD_LIBRARY_PATH', '')
    if opus_lib_path not in current_dyld:
        if current_dyld:
            os.environ['DYLD_LIBRARY_PATH'] = f"{opus_lib_path}:{current_dyld}"
        else:
            os.environ['DYLD_LIBRARY_PATH'] = opus_lib_path

import numpy as np
import opuslib
import websockets
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
BACKEND_WS_URL = os.getenv('BACKEND_WS_URL', "ws://localhost:8000/v4/listen")
SAMPLE_RATE = 16000  # OMI device uses 16kHz
FRAME_SIZE = 320  # Opus frame size (20ms at 16kHz)
CHANNELS = 1
CODEC = "opus_fs320"

# Test user ID (uses '123' for LOCAL_DEVELOPMENT mode)
TEST_UID = os.getenv('TEST_USER_UID', '123')


def generate_synthetic_speech(duration_seconds: float = 10, sample_rate: int = 16000) -> bytes:
    """
    Generate synthetic audio that sounds like speech (for testing).
    Creates a simple tone with varying frequency to simulate speech patterns.
    """
    print(f"🎤 Generating {duration_seconds}s of synthetic audio at {sample_rate}Hz...")

    num_samples = int(duration_seconds * sample_rate)
    t = np.linspace(0, duration_seconds, num_samples)

    # Create a varying frequency signal (simulates speech prosody)
    # Base frequency around 200Hz with variation
    frequency = 200 + 50 * np.sin(2 * np.pi * 2 * t)  # 2Hz variation

    # Generate the tone
    audio = np.sin(2 * np.pi * frequency * t)

    # Add some envelope (amplitude variation) to simulate speech
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    audio = audio * envelope

    # Convert to 16-bit PCM
    audio_int16 = (audio * 32767).astype(np.int16)

    return audio_int16.tobytes()


def load_audio_file(file_path: str) -> bytes:
    """Load audio from a WAV file and convert to 16kHz mono PCM."""
    print(f"📂 Loading audio from {file_path}...")

    with wave.open(file_path, 'rb') as wav_file:
        # Get audio parameters
        channels = wav_file.getnchannels()
        sample_width = wav_file.getsampwidth()
        framerate = wav_file.getframerate()
        n_frames = wav_file.getnframes()

        print(f"   Original: {channels}ch, {sample_width*8}bit, {framerate}Hz, {n_frames} frames")

        # Read audio data
        audio_data = wav_file.readframes(n_frames)

    # Convert to numpy array
    if sample_width == 2:  # 16-bit
        audio = np.frombuffer(audio_data, dtype=np.int16)
    else:
        raise ValueError(f"Unsupported sample width: {sample_width}")

    # Convert stereo to mono if needed
    if channels == 2:
        audio = audio.reshape(-1, 2).mean(axis=1).astype(np.int16)
        print("   Converted stereo to mono")

    # Resample if needed (simple linear interpolation for testing)
    if framerate != SAMPLE_RATE:
        duration = len(audio) / framerate
        new_length = int(duration * SAMPLE_RATE)
        audio = np.interp(
            np.linspace(0, len(audio), new_length),
            np.arange(len(audio)),
            audio
        ).astype(np.int16)
        print(f"   Resampled from {framerate}Hz to {SAMPLE_RATE}Hz")

    return audio.tobytes()


def encode_pcm_to_opus(pcm_data: bytes, sample_rate: int = 16000, frame_size: int = 320) -> list:
    """
    Encode PCM16 audio to Opus frames.

    Returns list of Opus-encoded frames (bytes).
    """
    print(f"🔄 Encoding PCM to Opus (frame_size={frame_size})...")

    encoder = opuslib.Encoder(sample_rate, CHANNELS, opuslib.APPLICATION_VOIP)

    # Calculate frame duration in bytes
    frame_duration_bytes = frame_size * 2  # 16-bit = 2 bytes per sample

    opus_frames = []
    offset = 0

    while offset + frame_duration_bytes <= len(pcm_data):
        # Extract frame
        pcm_frame = pcm_data[offset:offset + frame_duration_bytes]

        try:
            # Encode to Opus
            opus_frame = encoder.encode(pcm_frame, frame_size)
            opus_frames.append(opus_frame)
        except Exception as e:
            print(f"   ⚠️  Encoding error at offset {offset}: {e}")
            break

        offset += frame_duration_bytes

    print(f"   ✅ Encoded {len(opus_frames)} Opus frames")
    return opus_frames


async def simulate_omi_device(audio_file: Optional[str] = None, duration: float = 10):
    """
    Simulate OMI device connecting to backend and sending audio.

    Args:
        audio_file: Path to WAV file to use, or None to generate synthetic audio
        duration: Duration in seconds if generating synthetic audio
    """
    print("\n" + "="*70)
    print("🎧 OMI Device Simulator")
    print("="*70 + "\n")

    # Step 1: Get audio data
    if audio_file:
        pcm_data = load_audio_file(audio_file)
    else:
        pcm_data = generate_synthetic_speech(duration, SAMPLE_RATE)

    # Step 2: Encode to Opus
    opus_frames = encode_pcm_to_opus(pcm_data, SAMPLE_RATE, FRAME_SIZE)

    if not opus_frames:
        print("❌ No audio frames to send")
        return

    # Step 3: Connect to WebSocket
    ws_url = f"{BACKEND_WS_URL}?uid={TEST_UID}&language=en&sample_rate={SAMPLE_RATE}&codec={CODEC}&channels={CHANNELS}"

    # For LOCAL_DEVELOPMENT mode, use ADMIN_KEY for authentication
    admin_key = os.getenv('ADMIN_KEY', 'dev_testing_key_12345')
    headers = {
        'Authorization': f'Bearer {admin_key}{TEST_UID}'
    }

    print(f"\n🔌 Connecting to WebSocket...")
    print(f"   URL: {ws_url}")
    print(f"   Auth: LOCAL_DEVELOPMENT mode (UID {TEST_UID})")

    try:
        async with websockets.connect(ws_url, extra_headers=headers) as websocket:
            print("   ✅ Connected!\n")

            # Task to receive messages from server
            async def receive_messages():
                try:
                    while True:
                        message = await websocket.recv()

                        # Handle different message types
                        if isinstance(message, str):
                            if message == "ping":
                                # Server heartbeat
                                continue

                            try:
                                data = json.loads(message)

                                # Handle different event types
                                if isinstance(data, dict):
                                    event_type = data.get('event_type')

                                    if event_type == 'service_status':
                                        status = data.get('status', '')
                                        status_text = data.get('status_text', '')
                                        print(f"📡 Status: {status} - {status_text}")

                                    elif event_type == 'memory_created':
                                        memory = data.get('memory', {})
                                        print(f"\n💾 Memory Created!")
                                        print(f"   ID: {memory.get('id')}")
                                        print(f"   Status: {memory.get('status')}")

                                    else:
                                        print(f"📨 Event: {event_type}")

                                # Handle transcript segments
                                elif isinstance(data, list):
                                    for segment in data:
                                        if 'text' in segment:
                                            speaker = f"Speaker {segment.get('speaker_id', '?')}"
                                            text = segment.get('text', '')
                                            start = segment.get('start', 0)
                                            end = segment.get('end', 0)
                                            print(f"🗣️  [{start:.1f}s - {end:.1f}s] {speaker}: {text}")

                            except json.JSONDecodeError:
                                print(f"📨 Text message: {message}")

                except websockets.exceptions.ConnectionClosed:
                    print("\n🔌 Connection closed by server")
                except Exception as e:
                    print(f"\n❌ Receive error: {e}")

            # Start receiving task
            receive_task = asyncio.create_task(receive_messages())

            # Step 4: Send audio frames
            print("🎵 Sending audio frames...")
            print(f"   Total frames: {len(opus_frames)}")
            print(f"   Frame size: {FRAME_SIZE} samples ({FRAME_SIZE/SAMPLE_RATE*1000:.1f}ms)")
            print()

            frames_sent = 0
            start_time = asyncio.get_event_loop().time()

            for i, opus_frame in enumerate(opus_frames):
                # Send frame as bytes
                await websocket.send(opus_frame)
                frames_sent += 1

                # Progress indicator every 50 frames
                if (i + 1) % 50 == 0:
                    elapsed = asyncio.get_event_loop().time() - start_time
                    print(f"   Sent {frames_sent}/{len(opus_frames)} frames ({elapsed:.1f}s elapsed)")

                # Simulate real-time sending (20ms per frame for fs320)
                await asyncio.sleep(0.020)

            print(f"\n✅ Sent all {frames_sent} audio frames")

            # Wait a bit for final transcriptions
            print("\n⏳ Waiting for final transcriptions...")
            await asyncio.sleep(5)

            # Cancel receive task
            receive_task.cancel()
            try:
                await receive_task
            except asyncio.CancelledError:
                pass

            print("\n✅ Test completed!")

    except websockets.exceptions.InvalidURI:
        print(f"❌ Invalid WebSocket URI: {ws_url}")
    except ConnectionRefusedError:
        print(f"❌ Connection refused - is the backend running on localhost:8000?")
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()


def main():
    """Main entry point."""
    import argparse

    parser = argparse.ArgumentParser(description="Simulate OMI device sending audio to backend")
    parser.add_argument('--audio-file', type=str, help='Path to WAV file to use (optional)')
    parser.add_argument('--duration', type=float, default=10, help='Duration in seconds for synthetic audio (default: 10)')
    parser.add_argument('--uid', type=str, help='Test user UID (overrides TEST_USER_UID env var)')

    args = parser.parse_args()

    # Override TEST_UID if provided
    global TEST_UID
    if args.uid:
        TEST_UID = args.uid

    # Check if backend is likely running
    if not os.getenv('DEEPGRAM_API_KEY'):
        print("\n⚠️  Warning: DEEPGRAM_API_KEY not found in environment")
        print("   Make sure .env is loaded and backend is running\n")

    # Run the simulation
    asyncio.run(simulate_omi_device(args.audio_file, args.duration))


if __name__ == "__main__":
    main()
