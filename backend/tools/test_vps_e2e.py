#!/usr/bin/env python3
"""E2E test for Voice Mode v2 on VPS."""

import asyncio
import websockets
import struct
import math


def generate_speech_audio(duration_sec=3.0):
    """Generate speech-like audio."""
    sample_rate = 16000
    num_samples = int(sample_rate * duration_sec)
    samples = []

    for i in range(num_samples):
        t = i / sample_rate

        # Fundamental frequency varying like speech
        f0 = 150 + 50 * math.sin(2 * math.pi * 3 * t)

        # Generate voice
        phase = 2 * math.pi * f0 * t
        voice = 0.6 * math.sin(phase)
        voice += 0.3 * math.sin(2 * phase)
        voice += 0.1 * math.sin(3 * phase)

        # Amplitude envelope
        envelope = 0.3 + 0.7 * abs(math.sin(2 * math.pi * 4 * t))

        value = voice * envelope * 0.5
        sample = int(max(-1, min(1, value)) * 32767)
        samples.append(struct.pack('<h', sample))

    return b''.join(samples)


async def test():
    url = "ws://localhost:8000/v2/voice?uid=e2e-test&session_id=test-silero-fix"

    print("Generating speech audio...")
    audio = generate_speech_audio(duration_sec=3.0)
    print(f"Generated {len(audio)} bytes of audio")

    # Add 2 seconds of silence at end for VAD to trigger
    silence = b'\x00' * (16000 * 2 * 2)  # 2 seconds
    audio += silence
    print(f"Total with silence: {len(audio)} bytes")

    print("Connecting...")
    async with websockets.connect(url, close_timeout=30) as ws:
        print("Connected! Sending audio...")

        # Send audio in real-time chunks
        chunk_size = 3200  # 100ms at 16kHz, 16-bit
        chunks_sent = 0
        for i in range(0, len(audio), chunk_size):
            chunk = audio[i:i+chunk_size]
            await ws.send(chunk)
            chunks_sent += 1
            await asyncio.sleep(0.1)  # Real-time pacing

        print(f"Sent {chunks_sent} chunks in {chunks_sent * 0.1:.1f}s")
        print("Waiting for response...")

        # Wait for TTS response
        bytes_received = 0
        try:
            while True:
                response = await asyncio.wait_for(ws.recv(), timeout=15)
                if isinstance(response, bytes):
                    bytes_received += len(response)
                    print(f"Received audio: {len(response)} bytes (total: {bytes_received})")
        except asyncio.TimeoutError:
            pass

        if bytes_received > 0:
            print(f"\n*** SUCCESS! Received {bytes_received} bytes of TTS audio ***")
            return True
        else:
            print("\n*** FAILED: No audio received ***")
            return False


if __name__ == "__main__":
    result = asyncio.run(test())
    exit(0 if result else 1)
