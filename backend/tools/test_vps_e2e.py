#!/usr/bin/env python3
"""
E2E test for Voice Mode v2 on VPS.

Uses real audio samples instead of synthetic audio.

Usage:
    # Test against production VPS with SSL
    python tools/test_vps_e2e.py

    # Test against local server
    python tools/test_vps_e2e.py --local

    # Use specific audio file
    python tools/test_vps_e2e.py --audio test_audio/silero_test.wav
"""

import asyncio
import argparse
import os
import ssl
import wave
import certifi

try:
    import websockets
except ImportError:
    print("Please install websockets: pip install websockets")
    exit(1)


def load_audio_file(filepath: str) -> bytes:
    """Load audio from WAV file (must be 16kHz mono PCM16)."""
    with wave.open(filepath, 'rb') as w:
        if w.getframerate() != 16000:
            raise ValueError(f"Expected 16kHz, got {w.getframerate()}Hz. Resample audio first.")
        if w.getnchannels() != 1:
            raise ValueError(f"Expected mono, got {w.getnchannels()} channels")
        if w.getsampwidth() != 2:
            raise ValueError(f"Expected 16-bit (2 bytes), got {w.getsampwidth()} bytes")
        return w.readframes(w.getnframes())


async def test(args):
    """Run E2E test against voice mode endpoint."""

    # Determine URL
    if args.local:
        url = f"ws://localhost:8000/v2/voice?uid={args.uid}&session_id=e2e-test-real-audio"
        ssl_context = None
    else:
        url = f"wss://api.ella-ai-care.com/v2/voice?uid={args.uid}&session_id=e2e-test-real-audio"
        # Use certifi for SSL
        ssl_context = ssl.create_default_context(cafile=certifi.where())

    # Load real audio
    audio_path = args.audio
    if not os.path.isabs(audio_path):
        audio_path = os.path.join(os.path.dirname(__file__), '..', audio_path)

    print(f"Loading audio from {audio_path}...")
    try:
        audio = load_audio_file(audio_path)
        duration = len(audio) / 32000  # 16kHz * 2 bytes
        print(f"Loaded {len(audio)} bytes ({duration:.1f} seconds)")
    except Exception as e:
        print(f"Error loading audio: {e}")
        return False

    # Trim to first N seconds for faster testing
    max_seconds = args.duration
    max_bytes = int(max_seconds * 32000)
    if len(audio) > max_bytes:
        audio = audio[:max_bytes]
        print(f"Trimmed to first {max_seconds} seconds ({len(audio)} bytes)")

    # Add 2 seconds of silence at end for VAD to trigger
    silence = b'\x00' * (16000 * 2 * 2)  # 2 seconds @ 16kHz, 16-bit
    audio += silence
    print(f"Added 2s silence. Total: {len(audio)} bytes")

    print(f"\nConnecting to {url}...")
    try:
        async with websockets.connect(url, ssl=ssl_context, close_timeout=30) as ws:
            print("Connected! Sending audio...")

            # Send audio in real-time chunks (100ms each)
            chunk_size = 3200  # 100ms at 16kHz, 16-bit
            chunks_sent = 0

            for i in range(0, len(audio), chunk_size):
                chunk = audio[i:i+chunk_size]
                await ws.send(chunk)
                chunks_sent += 1
                if chunks_sent % 10 == 0:  # Log every second
                    print(f"  Sent {chunks_sent * 0.1:.1f}s...")
                await asyncio.sleep(0.1)  # Real-time pacing

            print(f"Sent {chunks_sent} chunks ({chunks_sent * 0.1:.1f}s)")
            print("Waiting for response...")

            # Wait for TTS response
            bytes_received = 0
            try:
                while True:
                    response = await asyncio.wait_for(ws.recv(), timeout=args.timeout)
                    if isinstance(response, bytes):
                        bytes_received += len(response)
                        print(f"  Received audio: {len(response)} bytes (total: {bytes_received})")
                    else:
                        print(f"  Received text: {response[:100]}...")
            except asyncio.TimeoutError:
                pass

            if bytes_received > 0:
                print(f"\n*** SUCCESS! Received {bytes_received} bytes of TTS audio ***")
                return True
            else:
                print("\n*** FAILED: No audio received ***")
                print("Check VPS logs: ssh root@100.101.168.91 'journalctl -u omi-backend -n 50 --no-pager'")
                return False

    except Exception as e:
        print(f"\nConnection error: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(description="E2E test for Voice Mode v2")
    parser.add_argument("--local", action="store_true",
                        help="Test against localhost instead of production")
    parser.add_argument("--audio", default="test_audio/silero_test.wav",
                        help="Path to audio file (16kHz mono WAV)")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="Max audio duration in seconds (default: 5)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Response timeout in seconds (default: 20)")
    parser.add_argument("--uid", default="e2e-test-user",
                        help="User ID for testing")
    args = parser.parse_args()

    result = asyncio.run(test(args))
    exit(0 if result else 1)


if __name__ == "__main__":
    main()
