import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Optional


class ProviderAudioSendRejected(RuntimeError):
    """Raised when an STT provider declines an audio frame."""


@dataclass
class SttSessionDeliveryReceipt:
    ingress_frames: int = 0
    ingress_encoded_bytes: int = 0
    decoded_frames: int = 0
    decoded_pcm_bytes: int = 0
    decode_errors: int = 0
    pcm_peak_abs: int = 0
    signal_frames_above_floor: int = 0
    provider_send_attempts: int = 0
    provider_send_accepted: int = 0
    provider_send_rejected: int = 0
    provider_pcm_bytes_accepted: int = 0
    provider_open_events: int = 0
    provider_empty_results: int = 0
    provider_interim_results: int = 0
    provider_final_results: int = 0
    provider_error_events: int = 0
    provider_close_events: int = 0
    transcript_callbacks: int = 0
    transcript_segments: int = 0
    drain_requested: bool = False
    drain_completed: bool = False
    _last_progress_at: float = field(default_factory=time.monotonic, repr=False)

    def record_ingress(self, encoded_bytes: int) -> None:
        self.ingress_frames += 1
        self.ingress_encoded_bytes += max(0, encoded_bytes)

    def record_decode_error(self) -> None:
        self.decode_errors += 1

    def record_decoded_pcm(self, pcm: bytes, *, sample_width: int = 2, signal_floor: int = 128) -> None:
        self.decoded_frames += 1
        self.decoded_pcm_bytes += len(pcm)
        if sample_width == 1:
            # PCM8 is unsigned. Normalize to the same signed 16-bit-equivalent
            # scale used by decoded PCM16 before comparing signal strength.
            peak = max((abs(int(sample) - 128) * 256 for sample in pcm), default=0)
            self.pcm_peak_abs = max(self.pcm_peak_abs, peak)
            if peak >= signal_floor:
                self.signal_frames_above_floor += 1
            return
        if sample_width != 2:
            raise ValueError("unsupported_pcm_sample_width")
        even_length = len(pcm) - (len(pcm) % 2)
        if even_length <= 0:
            return
        samples = memoryview(pcm[:even_length]).cast("h")
        peak = max((abs(int(sample)) for sample in samples), default=0)
        self.pcm_peak_abs = max(self.pcm_peak_abs, peak)
        if peak >= signal_floor:
            self.signal_frames_above_floor += 1

    def record_provider_send(self, pcm_bytes: int, *, accepted: bool) -> None:
        self.provider_send_attempts += 1
        if accepted:
            self.provider_send_accepted += 1
            self.provider_pcm_bytes_accepted += max(0, pcm_bytes)
        else:
            self.provider_send_rejected += 1

    def record_provider_event(self, result_type: Optional[str]) -> None:
        field_name = {
            "open": "provider_open_events",
            "empty": "provider_empty_results",
            "interim": "provider_interim_results",
            "final": "provider_final_results",
            "error": "provider_error_events",
            "closed": "provider_close_events",
        }.get(str(result_type or "").lower())
        if field_name:
            setattr(self, field_name, getattr(self, field_name) + 1)

    def record_transcript_callback(self, segment_count: int) -> None:
        self.transcript_callbacks += 1
        self.transcript_segments += max(0, segment_count)

    def progress_due(self, *, now: Optional[float] = None, interval_seconds: float = 30.0) -> bool:
        current = time.monotonic() if now is None else now
        if current - self._last_progress_at < interval_seconds:
            return False
        self._last_progress_at = current
        return True

    def snapshot(self, **terminal: Any) -> Dict[str, Any]:
        payload = asdict(self)
        payload.pop("_last_progress_at", None)
        payload.update(terminal)
        return payload


def forward_deepgram_audio(socket, chunk: bytes, receipt: SttSessionDeliveryReceipt) -> None:
    accepted = socket.send(chunk) is True
    receipt.record_provider_send(len(chunk), accepted=accepted)
    if not accepted:
        raise ProviderAudioSendRejected("stt_provider_send_rejected")


async def forward_async_provider_audio(send, chunk: bytes, receipt: SttSessionDeliveryReceipt) -> None:
    try:
        result = await send(chunk)
    except Exception:
        receipt.record_provider_send(len(chunk), accepted=False)
        raise ProviderAudioSendRejected("stt_provider_send_failed") from None
    accepted = result is not False
    receipt.record_provider_send(len(chunk), accepted=accepted)
    if not accepted:
        raise ProviderAudioSendRejected("stt_provider_send_rejected")
