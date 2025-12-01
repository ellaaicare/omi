"""
Raw PCM audio serializer for Pipecat.

This serializer handles raw PCM16 16kHz mono audio bytes
from iOS and other clients that send binary audio directly
over WebSocket without protobuf/JSON encoding.
"""

import json
from typing import Optional, Union

from pipecat.frames.frames import (
    Frame,
    StartFrame,
    InputAudioRawFrame,
    OutputAudioRawFrame,
)
from pipecat.serializers.base_serializer import FrameSerializer, FrameSerializerType


class RawPCMSerializer(FrameSerializer):
    """
    Serializer for raw PCM audio streams.

    iOS sends:
    - Binary messages: Raw PCM16 audio bytes (16kHz mono)
    - Text messages: JSON control messages (optional)

    Backend sends:
    - Binary messages: Raw PCM16 TTS audio bytes
    - Text messages: JSON status/events (optional)
    """

    def __init__(
        self,
        sample_rate: int = 16000,
        num_channels: int = 1,
    ):
        """
        Initialize the serializer.

        Args:
            sample_rate: Input audio sample rate (default 16kHz)
            num_channels: Number of audio channels (default 1 for mono)
        """
        super().__init__()
        self.sample_rate = sample_rate
        self.num_channels = num_channels

    @property
    def type(self) -> FrameSerializerType:
        """Return serializer type - we handle binary audio data."""
        return FrameSerializerType.BINARY

    def setup(self, frame: StartFrame):
        """Called when the pipeline starts."""
        pass  # No setup needed

    def deserialize(self, data: Union[str, bytes]) -> Optional[Frame]:
        """
        Convert incoming data to Pipecat frames.

        Binary data -> InputAudioRawFrame
        Text data -> Parse as JSON (for control messages)
        """
        if isinstance(data, bytes):
            # Raw PCM audio from iOS
            if len(data) == 0:
                return None

            return InputAudioRawFrame(
                audio=data,
                sample_rate=self.sample_rate,
                num_channels=self.num_channels,
            )

        elif isinstance(data, str):
            # Text message - try to parse as JSON control message
            try:
                msg = json.loads(data)
                msg_type = msg.get("type", "")

                # Handle control messages
                if msg_type == "ping":
                    # Ignore pings, they're for keepalive
                    return None
                elif msg_type == "stop":
                    # Client wants to stop - could return EndFrame
                    return None

                # Unknown message type - log and ignore
                print(f"⚠️ Unknown WebSocket message type: {msg_type}")
                return None

            except json.JSONDecodeError:
                # Not valid JSON - ignore
                print(f"⚠️ Received non-JSON text message: {data[:50]}...")
                return None

        return None

    def serialize(self, frame: Frame) -> Optional[Union[str, bytes]]:
        """
        Convert Pipecat frames to output data.

        OutputAudioRawFrame -> Raw PCM bytes
        Other frames -> JSON (if needed)
        """
        if isinstance(frame, OutputAudioRawFrame):
            # Send raw PCM audio back to iOS
            return frame.audio

        # For other frame types, we could serialize to JSON
        # but typically we only send audio back over WebSocket
        return None
