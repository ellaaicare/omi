#!/usr/bin/env python3
"""
Test script based on official play_sound_on_friend.py
Uses the same protocol with gain amplification
"""
import asyncio
import struct
import math
import numpy as np
from bleak import BleakClient, BleakScanner

# BLE UUIDs from official script
AUDIO_SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
SPEAKER_CHAR_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"

# Official protocol parameters
PACKET_SIZE = 400
GAIN = 5  # Official script uses 5x amplification
MAX_ALLOWED_SAMPLES = 50000

def generate_test_tone(frequency=440, duration=2.0, sample_rate=8000, gain=GAIN):
    """
    Generate test tone with gain amplification (like official script)
    """
    num_samples = int(sample_rate * duration)
    audio_data = []

    for i in range(num_samples):
        t = i / sample_rate
        sample = math.sin(2 * math.pi * frequency * t)
        # Scale to 16-bit and apply gain (official script does this)
        int_sample = int(sample * 16000)
        audio_data.append(int_sample)

    # Convert to numpy array and apply gain
    audio_array = np.array(audio_data, dtype=np.int16)
    amplified = audio_array * gain

    # Clip to prevent overflow
    amplified = np.clip(amplified, -32767, 32767).astype(np.int16)

    return amplified.tobytes()

async def find_omi_device():
    """Scan for Omi DevKit device"""
    print("Scanning for Omi DevKit...")
    devices = await BleakScanner.discover(timeout=5.0)

    for device in devices:
        if device.name and ('Omi' in device.name or 'Friend' in device.name or 'DevKit' in device.name):
            print(f"Found: {device.name} ({device.address})")
            return device.address

    print("No Omi device found. Devices seen:")
    for device in devices:
        print(f"  {device.name}: {device.address}")
    return None

async def test_speaker_official_protocol(device_address):
    """
    Test speaker using official play_sound_on_friend.py protocol
    with notify callbacks for chunk acknowledgement
    """
    print(f"\nConnecting to {device_address}...")

    remaining_bytes = 0
    total_offset = 0
    audio_data = None

    async with BleakClient(device_address, timeout=20.0) as client:
        if not client.is_connected:
            print("Failed to connect!")
            return

        print(f"Connected! MTU: {client.mtu_size}")

        # List services (like official script)
        print("\nAvailable services:")
        for service in client.services:
            print(f"  Service: {service.uuid}")

        # Generate test tone with gain (440Hz for 2 seconds)
        print(f"\nGenerating 440Hz test tone with {GAIN}x gain...")
        audio_data = generate_test_tone(frequency=440, duration=2.0, gain=GAIN)
        remaining_bytes = len(audio_data)

        print(f"Generated {remaining_bytes} bytes of audio data")

        if remaining_bytes > MAX_ALLOWED_SAMPLES:
            print(f"❌ Audio too large ({remaining_bytes} > {MAX_ALLOWED_SAMPLES}). Exiting.")
            return

        # Notify callback (official script uses this to send chunks)
        async def on_notify(sender, data: bytearray):
            nonlocal remaining_bytes, total_offset, audio_data

            # Official script reads response
            try:
                response = np.frombuffer(data, dtype=np.int16)[0]
                print(f"  Device response: {response}")
            except:
                print(f"  Device notify: {len(data)} bytes")

            # Send next chunk
            if remaining_bytes > PACKET_SIZE:
                start_idx = total_offset
                total_offset += PACKET_SIZE
                remaining_bytes -= PACKET_SIZE
                print(f"  Sending chunk {start_idx//PACKET_SIZE + 1}: bytes {start_idx} to {start_idx+PACKET_SIZE}")
                await client.write_gatt_char(SPEAKER_CHAR_UUID,
                                            audio_data[start_idx:start_idx+PACKET_SIZE],
                                            response=True)

            elif 0 < remaining_bytes <= PACKET_SIZE:
                print(f"  Sending final chunk: {remaining_bytes} bytes")
                start_idx = total_offset
                await client.write_gatt_char(SPEAKER_CHAR_UUID,
                                            audio_data[start_idx:start_idx+remaining_bytes],
                                            response=True)
                total_offset += remaining_bytes
                remaining_bytes = 0

            else:
                print("✅ Transfer complete!")

        # Start notifications (official protocol)
        print("\nStarting notification listener...")
        await client.start_notify(SPEAKER_CHAR_UUID, on_notify)
        await asyncio.sleep(1)

        # Send audio size (Step 1)
        size_bytes = np.array([len(audio_data)], dtype=np.uint32).tobytes()
        print(f"\nStep 1: Sending audio size: {len(audio_data)} bytes")
        await client.write_gatt_char(SPEAKER_CHAR_UUID, size_bytes, response=True)

        # Wait for chunks to send via notify callback
        print("\nStep 2: Waiting for chunks to send via notify callback...")

        # Wait for transfer to complete (with timeout)
        timeout_seconds = 30
        for i in range(timeout_seconds):
            if remaining_bytes == 0:
                break
            await asyncio.sleep(1)

        if remaining_bytes > 0:
            print(f"\n⚠️ Warning: Transfer incomplete ({remaining_bytes} bytes remaining)")

        print(f"\n✅ Sent {total_offset} total bytes")
        print("Speaker should be playing now...")
        await asyncio.sleep(4)

    print("Disconnected.")

async def main():
    """Main test function"""
    print("=== Omi DevKit2 Speaker Test (Official Protocol) ===\n")
    print(f"Using official protocol from play_sound_on_friend.py:")
    print(f"  - Gain: {GAIN}x amplification")
    print(f"  - Packet size: {PACKET_SIZE} bytes")
    print(f"  - Notify-based chunk transmission\n")

    # Find device
    device_address = await find_omi_device()
    if not device_address:
        print("\n❌ Could not find Omi device.")
        return

    # Test speaker
    try:
        await test_speaker_official_protocol(device_address)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    # Install numpy if needed: pip install numpy
    try:
        import numpy
    except ImportError:
        print("ERROR: numpy not installed. Run: pip install numpy")
        exit(1)

    asyncio.run(main())
