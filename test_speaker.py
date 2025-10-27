#!/usr/bin/env python3
"""
Test script to send audio to Omi DevKit2 speaker via BLE
"""
import asyncio
import struct
import math
from bleak import BleakClient, BleakScanner

# BLE UUIDs from firmware
AUDIO_SERVICE_UUID = "19B10000-E8F2-537E-4F6C-D104768A1214"
SPEAKER_CHAR_UUID = "19B10003-E8F2-537E-4F6C-D104768A1214"

def generate_test_tone(frequency=440, duration=2.0, sample_rate=8000):
    """
    Generate a simple sine wave test tone
    Args:
        frequency: Tone frequency in Hz (default 440Hz = A4 note)
        duration: Duration in seconds
        sample_rate: Sample rate in Hz (firmware uses 8000Hz)
    Returns:
        bytes: Audio data as 16-bit signed integers
    """
    num_samples = int(sample_rate * duration)
    audio_data = []

    for i in range(num_samples):
        # Generate sine wave
        t = i / sample_rate
        sample = math.sin(2 * math.pi * frequency * t)
        # Scale to 16-bit range (with some headroom to avoid clipping)
        int_sample = int(sample * 16000)  # Max 32767, using 16000 for safety
        audio_data.append(int_sample)

    # Pack as 16-bit signed integers (little-endian)
    return struct.pack(f'<{len(audio_data)}h', *audio_data)

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

async def test_speaker(device_address):
    """Connect to device and test speaker"""
    print(f"\nConnecting to {device_address}...")

    async with BleakClient(device_address, timeout=20.0) as client:
        if not client.is_connected:
            print("Failed to connect!")
            return

        print(f"Connected! MTU: {client.mtu_size}")

        # List available services/characteristics
        print("\nAvailable services:")
        for service in client.services:
            print(f"  Service: {service.uuid}")
            for char in service.characteristics:
                print(f"    Characteristic: {char.uuid} (Properties: {char.properties})")

        # Generate test tone (440Hz A note for 2 seconds)
        print("\nGenerating 440Hz test tone (2 seconds)...")
        audio_data = generate_test_tone(frequency=440, duration=2.0)
        print(f"Generated {len(audio_data)} bytes of audio data")

        # Step 1: Send audio size (4 bytes as uint32, little-endian)
        audio_size = len(audio_data)
        size_packet = struct.pack('<I', audio_size)  # 4-byte unsigned int
        print(f"\nStep 1: Sending audio size: {audio_size} bytes")
        await client.write_gatt_char(SPEAKER_CHAR_UUID, size_packet, response=True)
        await asyncio.sleep(0.1)

        # Step 2: Send audio data in 400-byte chunks
        CHUNK_SIZE = 400
        num_chunks = (len(audio_data) + CHUNK_SIZE - 1) // CHUNK_SIZE
        print(f"Step 2: Sending {num_chunks} chunks of audio data...")

        for i in range(0, len(audio_data), CHUNK_SIZE):
            chunk = audio_data[i:i+CHUNK_SIZE]
            chunk_num = i // CHUNK_SIZE + 1
            print(f"  Sending chunk {chunk_num}/{num_chunks} ({len(chunk)} bytes)...")
            await client.write_gatt_char(SPEAKER_CHAR_UUID, chunk, response=False)
            await asyncio.sleep(0.02)  # Small delay between chunks

        print("\n✅ Audio data sent! Speaker should be playing now...")
        print("Waiting for playback to complete (4 seconds)...")
        await asyncio.sleep(4)

    print("Disconnected.")

async def main():
    """Main test function"""
    print("=== Omi DevKit2 Speaker Test ===\n")

    # Find device
    device_address = await find_omi_device()
    if not device_address:
        print("\n❌ Could not find Omi device. Make sure:")
        print("  1. Device is powered on")
        print("  2. Bluetooth is enabled on this computer")
        print("  3. Device is not connected to another app")
        return

    # Test speaker
    try:
        await test_speaker(device_address)
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
