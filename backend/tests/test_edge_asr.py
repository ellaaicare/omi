#!/usr/bin/env python3
"""
Test Suite for Edge ASR Integration

Tests the backend's ability to receive and process pre-transcribed text
from iOS on-device ASR (Apple Speech Framework / Parakeet).

Run with: pytest tests/test_edge_asr.py -v
"""

import asyncio
import json
import pytest
import websockets
from datetime import datetime


# Test configuration
WS_BASE_URL = "ws://localhost:8000/v4/listen"
TEST_UID = "HbBdbnRkPJhpYFIIsd34krM8FKD3"  # User 0 from test data
ADMIN_KEY = "test-admin-key-local-dev-only"


def get_ws_url(uid=TEST_UID, language="en", sample_rate=16000, codec="opus", channels=1):
    """Build WebSocket URL with query parameters"""
    return f"{WS_BASE_URL}?uid={uid}&language={language}&sample_rate={sample_rate}&codec={codec}&channels={channels}"


def get_auth_headers(uid=TEST_UID):
    """Get authentication headers for local development"""
    return {"Authorization": f"{ADMIN_KEY}{uid}"}


# Test data
VALID_SEGMENT = {
    "type": "transcript_segment",
    "text": "I went for a walk in the park this morning",
    "speaker": "SPEAKER_00",
    "start": 0.0,
    "end": 3.5,
    "is_final": True,
    "confidence": 0.95
}

MINIMAL_SEGMENT = {
    "type": "transcript_segment",
    "text": "Hello world"
}

EMPTY_TEXT_SEGMENT = {
    "type": "transcript_segment",
    "text": "",
    "speaker": "SPEAKER_00"
}

WHITESPACE_ONLY_SEGMENT = {
    "type": "transcript_segment",
    "text": "   ",
    "speaker": "SPEAKER_00"
}


class TestEdgeASRBasic:
    """Basic edge ASR functionality tests"""

    @pytest.mark.asyncio
    async def test_websocket_connection(self):
        """Test that WebSocket connection accepts edge ASR clients"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            assert websocket.open
            await websocket.close()

    @pytest.mark.asyncio
    async def test_send_valid_segment(self):
        """Test sending a valid transcript segment"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            # Send valid segment
            await websocket.send(json.dumps(VALID_SEGMENT))

            # Wait for processing
            await asyncio.sleep(0.5)

            # Connection should still be open
            assert websocket.open

    @pytest.mark.asyncio
    async def test_send_minimal_segment(self):
        """Test sending segment with only required fields"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            # Send minimal segment (only type and text)
            await websocket.send(json.dumps(MINIMAL_SEGMENT))

            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_send_empty_text(self):
        """Test that empty text segments are ignored gracefully"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            # Send segment with empty text
            await websocket.send(json.dumps(EMPTY_TEXT_SEGMENT))

            await asyncio.sleep(0.5)
            # Should not crash, connection stays open
            assert websocket.open

    @pytest.mark.asyncio
    async def test_send_whitespace_only(self):
        """Test that whitespace-only text is ignored"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(WHITESPACE_ONLY_SEGMENT))

            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRMultipleSegments:
    """Test sending multiple segments in sequence"""

    @pytest.mark.asyncio
    async def test_send_three_segments(self):
        """Test sending 3 segments to simulate real conversation"""
        url = get_ws_url()
        headers = get_auth_headers()

        segments = [
            {
                "type": "transcript_segment",
                "text": "I went for a walk in the park",
                "speaker": "SPEAKER_00",
                "start": 0.0,
                "end": 3.0
            },
            {
                "type": "transcript_segment",
                "text": "The weather was really nice",
                "speaker": "SPEAKER_00",
                "start": 3.0,
                "end": 5.5
            },
            {
                "type": "transcript_segment",
                "text": "I saw some birds and squirrels",
                "speaker": "SPEAKER_00",
                "start": 5.5,
                "end": 8.0
            }
        ]

        async with websockets.connect(url, extra_headers=headers) as websocket:
            for segment in segments:
                await websocket.send(json.dumps(segment))
                await asyncio.sleep(0.6)  # 600ms chunks

            # Wait for final processing
            await asyncio.sleep(1.0)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_rapid_segments(self):
        """Test sending segments rapidly (no delay)"""
        url = get_ws_url()
        headers = get_auth_headers()

        segments = [
            {"type": "transcript_segment", "text": f"Segment {i}"}
            for i in range(5)
        ]

        async with websockets.connect(url, extra_headers=headers) as websocket:
            for segment in segments:
                await websocket.send(json.dumps(segment))

            await asyncio.sleep(1.0)
            assert websocket.open


class TestEdgeASRSpeakerHandling:
    """Test speaker identification in edge ASR"""

    @pytest.mark.asyncio
    async def test_single_speaker(self):
        """Test conversation with single speaker"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "This is a single speaker conversation",
            "speaker": "SPEAKER_00"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_no_speaker_field(self):
        """Test segment without speaker field (should default to SPEAKER_00)"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "Segment with no speaker field"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRTimestamps:
    """Test timestamp handling"""

    @pytest.mark.asyncio
    async def test_with_timestamps(self):
        """Test segment with start and end timestamps"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "Timestamped segment",
            "start": 1.5,
            "end": 4.2
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_without_timestamps(self):
        """Test segment without timestamps (should default to 0)"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "No timestamp segment"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRConfidence:
    """Test confidence score handling"""

    @pytest.mark.asyncio
    async def test_high_confidence(self):
        """Test segment with high confidence score"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "High confidence transcription",
            "confidence": 0.98
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_low_confidence(self):
        """Test segment with low confidence score"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "Low confidence transcription",
            "confidence": 0.65
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRLanguages:
    """Test different language support"""

    @pytest.mark.asyncio
    async def test_english(self):
        """Test English language edge ASR"""
        url = get_ws_url(language="en")
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "This is an English conversation"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_spanish(self):
        """Test Spanish language edge ASR"""
        url = get_ws_url(language="es")
        headers = get_auth_headers()

        segment = {
            "type": "transcript_segment",
            "text": "Esta es una conversación en español"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRErrorHandling:
    """Test error handling and edge cases"""

    @pytest.mark.asyncio
    async def test_invalid_json(self):
        """Test sending invalid JSON (should be handled gracefully)"""
        url = get_ws_url()
        headers = get_auth_headers()

        async with websockets.connect(url, extra_headers=headers) as websocket:
            # Send invalid JSON
            try:
                await websocket.send("not valid json {{{")
                await asyncio.sleep(0.5)
            except:
                pass  # Expected to potentially fail

    @pytest.mark.asyncio
    async def test_missing_type_field(self):
        """Test segment without 'type' field (should be ignored)"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "text": "No type field",
            "speaker": "SPEAKER_00"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open

    @pytest.mark.asyncio
    async def test_wrong_type_field(self):
        """Test segment with wrong type (should be ignored)"""
        url = get_ws_url()
        headers = get_auth_headers()

        segment = {
            "type": "wrong_type",
            "text": "Wrong type field"
        }

        async with websockets.connect(url, extra_headers=headers) as websocket:
            await websocket.send(json.dumps(segment))
            await asyncio.sleep(0.5)
            assert websocket.open


class TestEdgeASRIntegration:
    """Integration tests for end-to-end flow"""

    @pytest.mark.asyncio
    async def test_full_conversation_flow(self):
        """Test a complete conversation with edge ASR"""
        url = get_ws_url()
        headers = get_auth_headers()

        conversation = [
            {
                "type": "transcript_segment",
                "text": "Hey, how are you doing today?",
                "speaker": "SPEAKER_00",
                "start": 0.0,
                "end": 2.5,
                "confidence": 0.95
            },
            {
                "type": "transcript_segment",
                "text": "I had a really interesting morning",
                "speaker": "SPEAKER_00",
                "start": 2.5,
                "end": 5.0,
                "confidence": 0.92
            },
            {
                "type": "transcript_segment",
                "text": "I went to the doctor for a checkup",
                "speaker": "SPEAKER_00",
                "start": 5.0,
                "end": 7.5,
                "confidence": 0.88
            },
            {
                "type": "transcript_segment",
                "text": "Everything looks good, blood pressure is normal",
                "speaker": "SPEAKER_00",
                "start": 7.5,
                "end": 10.5,
                "confidence": 0.94
            }
        ]

        async with websockets.connect(url, extra_headers=headers) as websocket:
            for segment in conversation:
                await websocket.send(json.dumps(segment))
                await asyncio.sleep(0.6)  # 600ms chunks

            # Wait for processing
            await asyncio.sleep(2.0)

            # Send stop signal
            await websocket.send(json.dumps({"type": "stop"}))
            await asyncio.sleep(1.0)


# Helper for manual testing
async def manual_test():
    """Quick manual test for development"""
    print("=" * 80)
    print("EDGE ASR MANUAL TEST")
    print("=" * 80)

    url = get_ws_url()
    headers = get_auth_headers()

    print(f"\nConnecting to: {url}")

    async with websockets.connect(url, extra_headers=headers) as websocket:
        print("✅ Connected!")

        # Send test segment
        segment = {
            "type": "transcript_segment",
            "text": "This is a manual test of edge ASR",
            "speaker": "SPEAKER_00",
            "start": 0.0,
            "end": 3.0,
            "confidence": 0.95
        }

        print(f"\n📤 Sending: {segment['text']}")
        await websocket.send(json.dumps(segment))

        await asyncio.sleep(1.0)
        print("✅ Test complete!")


if __name__ == "__main__":
    # Run manual test if executed directly
    asyncio.run(manual_test())
