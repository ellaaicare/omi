#!/usr/bin/env python3
"""
Comprehensive Silero VAD Test Suite.

Tests the Silero VAD neural network with various audio scenarios:
1. Clean speech
2. Background noise only (should NOT trigger VAD)
3. Speech + background noise (should trigger VAD)
4. Quiet speech
5. Varying noise levels

This validates that Silero properly distinguishes speech from noise,
unlike a simple RMS threshold.

Usage:
    python -m integrations.pipecat.tests.test_silero_vad

Requirements:
    pip install pipecat-ai[silero] numpy
"""

import sys
import os
import struct
import math
import time
from typing import Tuple, List
from dataclasses import dataclass

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(__file__)))))

try:
    import numpy as np
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.audio.vad.vad_analyzer import VADParams, VADState
except ImportError as e:
    print(f"Missing dependencies: {e}")
    print("Install with: pip install pipecat-ai[silero] numpy")
    sys.exit(1)


@dataclass
class TestResult:
    """Result of a VAD test."""
    name: str
    passed: bool
    expected_speech: bool
    detected_speech: bool
    avg_confidence: float
    max_confidence: float
    details: str = ""


class AudioGenerator:
    """Generate various audio samples for testing."""

    SAMPLE_RATE = 16000
    FRAME_SIZE = 512  # Silero needs exactly 512 samples at 16kHz

    @classmethod
    def to_bytes(cls, samples: np.ndarray) -> bytes:
        """Convert float32 samples to PCM16 bytes."""
        # Clip to [-1, 1] and convert to int16
        samples = np.clip(samples, -1, 1)
        int_samples = (samples * 32767).astype(np.int16)
        return int_samples.tobytes()

    @classmethod
    def generate_silence(cls, duration_sec: float = 1.0) -> bytes:
        """Generate silence (zero samples)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        samples = np.zeros(num_samples, dtype=np.float32)
        return cls.to_bytes(samples)

    @classmethod
    def generate_white_noise(cls, duration_sec: float = 1.0, amplitude: float = 0.1) -> bytes:
        """Generate white noise (random samples)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        samples = np.random.uniform(-amplitude, amplitude, num_samples).astype(np.float32)
        return cls.to_bytes(samples)

    @classmethod
    def generate_pink_noise(cls, duration_sec: float = 1.0, amplitude: float = 0.1) -> bytes:
        """Generate pink noise approximation (low-passed white noise)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        # Simple pink noise approximation using running average
        white = np.random.randn(num_samples)
        # Low-pass filter approximation
        pink = np.zeros(num_samples)
        alpha = 0.9  # Smoothing factor
        pink[0] = white[0]
        for i in range(1, num_samples):
            pink[i] = alpha * pink[i-1] + (1-alpha) * white[i]
        # Normalize and scale
        pink = pink / (np.max(np.abs(pink)) + 1e-10) * amplitude
        return cls.to_bytes(pink.astype(np.float32))

    @classmethod
    def generate_hvac_noise(cls, duration_sec: float = 1.0, amplitude: float = 0.05) -> bytes:
        """Generate HVAC-like noise (low frequency rumble + white noise)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        t = np.linspace(0, duration_sec, num_samples)

        # Low frequency rumble (60Hz + harmonics)
        rumble = 0.5 * np.sin(2 * np.pi * 60 * t)
        rumble += 0.3 * np.sin(2 * np.pi * 120 * t)
        rumble += 0.2 * np.sin(2 * np.pi * 180 * t)

        # Add some white noise
        noise = np.random.uniform(-0.3, 0.3, num_samples)

        # Combine and scale
        samples = (rumble + noise) * amplitude
        return cls.to_bytes(samples.astype(np.float32))

    @classmethod
    def generate_keyboard_typing(cls, duration_sec: float = 1.0) -> bytes:
        """Generate keyboard typing sounds (impulse-like clicks)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        samples = np.zeros(num_samples, dtype=np.float32)

        # Add random click impulses (5-10 per second)
        num_clicks = int(7 * duration_sec)
        click_positions = np.random.randint(0, num_samples, num_clicks)

        for pos in click_positions:
            # Each click is a short decaying impulse
            click_len = min(100, num_samples - pos)
            decay = np.exp(-np.linspace(0, 5, click_len))
            click = np.random.randn(click_len) * decay * 0.3
            samples[pos:pos + click_len] += click

        return cls.to_bytes(samples)

    @classmethod
    def generate_speech_formants(cls, duration_sec: float = 1.0, amplitude: float = 0.5) -> bytes:
        """Generate speech-like formant patterns (realistic speech simulation)."""
        num_samples = int(cls.SAMPLE_RATE * duration_sec)
        t = np.linspace(0, duration_sec, num_samples)

        # Fundamental frequency (pitch) - varying between 100-200 Hz
        f0 = 150 + 50 * np.sin(2 * np.pi * 3 * t)

        # Generate voiced excitation (glottal pulse train)
        phase = np.cumsum(f0 / cls.SAMPLE_RATE)
        glottal = np.sin(2 * np.pi * phase)

        # Add harmonics
        glottal += 0.5 * np.sin(4 * np.pi * phase)
        glottal += 0.25 * np.sin(6 * np.pi * phase)

        # Formant frequencies (vowels)
        f1 = 500 + 200 * np.sin(2 * np.pi * 1.5 * t)  # First formant
        f2 = 1500 + 500 * np.sin(2 * np.pi * 2 * t)   # Second formant

        # Apply formant-like modulation
        formant1 = np.sin(2 * np.pi * f1 * t / cls.SAMPLE_RATE)
        formant2 = np.sin(2 * np.pi * f2 * t / cls.SAMPLE_RATE)

        # Combine with amplitude envelope (speech rhythm)
        envelope = 0.3 + 0.7 * np.abs(np.sin(2 * np.pi * 4 * t))

        samples = (0.6 * glottal + 0.3 * formant1 + 0.1 * formant2) * envelope * amplitude
        return cls.to_bytes(samples.astype(np.float32))

    @classmethod
    def mix_audio(cls, speech_bytes: bytes, noise_bytes: bytes, snr_db: float = 10) -> bytes:
        """Mix speech and noise at specified SNR."""
        # Convert to float arrays
        speech = np.frombuffer(speech_bytes, dtype=np.int16).astype(np.float32) / 32767
        noise = np.frombuffer(noise_bytes, dtype=np.int16).astype(np.float32) / 32767

        # Ensure same length
        min_len = min(len(speech), len(noise))
        speech = speech[:min_len]
        noise = noise[:min_len]

        # Calculate scaling for target SNR
        speech_power = np.mean(speech ** 2)
        noise_power = np.mean(noise ** 2)

        if noise_power > 0:
            target_noise_power = speech_power / (10 ** (snr_db / 10))
            noise_scale = np.sqrt(target_noise_power / noise_power)
            noise = noise * noise_scale

        # Mix
        mixed = speech + noise
        mixed = np.clip(mixed, -1, 1)

        return cls.to_bytes(mixed)

    @classmethod
    def load_wav_file(cls, filepath: str) -> bytes:
        """Load audio from WAV file."""
        import wave
        with wave.open(filepath, 'rb') as w:
            if w.getframerate() != 16000:
                raise ValueError(f"Expected 16kHz, got {w.getframerate()}")
            if w.getnchannels() != 1:
                raise ValueError(f"Expected mono, got {w.getnchannels()} channels")
            return w.readframes(w.getnframes())


class SileroVADTester:
    """Test Silero VAD with various audio scenarios."""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.vad = SileroVADAnalyzer(
            sample_rate=16000,
            params=VADParams(
                confidence=0.6,
                start_secs=0.2,
                stop_secs=0.8,
                min_volume=0.4,
            )
        )
        self.vad.set_sample_rate(16000)
        self.frame_size = self.vad.num_frames_required() * 2  # 1024 bytes

    def analyze_audio(self, audio_bytes: bytes) -> Tuple[List[float], bool]:
        """
        Analyze audio and return confidence scores.

        Returns:
            Tuple of (confidence scores list, speech detected bool)
        """
        confidences = []
        speech_frames = 0
        total_frames = 0

        # Process frame by frame
        for i in range(0, len(audio_bytes) - self.frame_size, self.frame_size):
            frame = audio_bytes[i:i + self.frame_size]
            confidence = self.vad.voice_confidence(frame)
            confidences.append(confidence)
            total_frames += 1

            if confidence >= self.vad._params.confidence:
                speech_frames += 1

            if self.verbose and total_frames % 10 == 0:
                print(f"  Frame {total_frames}: confidence={confidence:.3f}")

        # Consider speech detected if >10% of frames exceed threshold
        # Real speech has pauses, so 10% is more realistic
        speech_detected = speech_frames > total_frames * 0.1

        return confidences, speech_detected

    def run_test(self, name: str, audio_bytes: bytes, expected_speech: bool) -> TestResult:
        """Run a single VAD test."""
        print(f"\n{'='*60}")
        print(f"Test: {name}")
        print(f"Expected: {'SPEECH' if expected_speech else 'NO SPEECH'}")
        print(f"Audio length: {len(audio_bytes)} bytes ({len(audio_bytes)/32:.0f}ms)")

        confidences, detected = self.analyze_audio(audio_bytes)

        avg_conf = float(sum(confidences) / len(confidences)) if confidences else 0.0
        max_conf = float(max(confidences)) if confidences else 0.0

        passed = detected == expected_speech

        print(f"Result: {'SPEECH' if detected else 'NO SPEECH'}")
        print(f"Avg confidence: {avg_conf:.3f}")
        print(f"Max confidence: {max_conf:.3f}")
        print(f"Status: {'PASS' if passed else 'FAIL'}")

        return TestResult(
            name=name,
            passed=passed,
            expected_speech=expected_speech,
            detected_speech=detected,
            avg_confidence=avg_conf,
            max_confidence=max_conf,
        )


def run_all_tests(verbose: bool = False) -> List[TestResult]:
    """Run all VAD tests."""
    print("=" * 60)
    print("Silero VAD Comprehensive Test Suite")
    print("=" * 60)

    tester = SileroVADTester(verbose=verbose)
    gen = AudioGenerator()
    results = []

    # Test 1: Pure silence
    print("\n[1/10] Testing pure silence...")
    audio = gen.generate_silence(duration_sec=2.0)
    results.append(tester.run_test(
        "Pure Silence",
        audio,
        expected_speech=False
    ))

    # Test 2: White noise (low level)
    print("\n[2/10] Testing white noise (low)...")
    audio = gen.generate_white_noise(duration_sec=2.0, amplitude=0.05)
    results.append(tester.run_test(
        "White Noise (5%)",
        audio,
        expected_speech=False
    ))

    # Test 3: White noise (high level)
    print("\n[3/10] Testing white noise (high)...")
    audio = gen.generate_white_noise(duration_sec=2.0, amplitude=0.3)
    results.append(tester.run_test(
        "White Noise (30%)",
        audio,
        expected_speech=False
    ))

    # Test 4: HVAC noise
    print("\n[4/10] Testing HVAC noise...")
    audio = gen.generate_hvac_noise(duration_sec=2.0, amplitude=0.1)
    results.append(tester.run_test(
        "HVAC Noise",
        audio,
        expected_speech=False
    ))

    # Test 5: Keyboard typing
    print("\n[5/10] Testing keyboard typing...")
    audio = gen.generate_keyboard_typing(duration_sec=2.0)
    results.append(tester.run_test(
        "Keyboard Typing",
        audio,
        expected_speech=False
    ))

    # Test 6: Clean speech (synthetic)
    print("\n[6/10] Testing synthetic speech...")
    audio = gen.generate_speech_formants(duration_sec=2.0, amplitude=0.5)
    results.append(tester.run_test(
        "Synthetic Speech",
        audio,
        expected_speech=True
    ))

    # Test 7: Quiet speech
    print("\n[7/10] Testing quiet speech...")
    audio = gen.generate_speech_formants(duration_sec=2.0, amplitude=0.15)
    results.append(tester.run_test(
        "Quiet Speech",
        audio,
        expected_speech=True
    ))

    # Test 8: Speech + HVAC noise (10dB SNR)
    print("\n[8/10] Testing speech + HVAC noise...")
    speech = gen.generate_speech_formants(duration_sec=2.0, amplitude=0.5)
    noise = gen.generate_hvac_noise(duration_sec=2.0, amplitude=0.1)
    audio = gen.mix_audio(speech, noise, snr_db=10)
    results.append(tester.run_test(
        "Speech + HVAC (10dB SNR)",
        audio,
        expected_speech=True
    ))

    # Test 9: Speech + keyboard (5dB SNR)
    print("\n[9/10] Testing speech + keyboard typing...")
    speech = gen.generate_speech_formants(duration_sec=2.0, amplitude=0.5)
    noise = gen.generate_keyboard_typing(duration_sec=2.0)
    audio = gen.mix_audio(speech, noise, snr_db=5)
    results.append(tester.run_test(
        "Speech + Keyboard (5dB SNR)",
        audio,
        expected_speech=True
    ))

    # Test 10: Real audio file (if available)
    print("\n[10/10] Testing real audio file...")
    try:
        audio_file = os.path.join(
            os.path.dirname(__file__),
            "..", "..", "..",
            "test_audio", "pyannote_sample.wav"
        )
        audio = gen.load_wav_file(audio_file)
        # Skip first 7 seconds (silence in sample - speech starts at second 7)
        audio = audio[7 * 32000:]  # Skip 7s at 16kHz, 16-bit
        results.append(tester.run_test(
            "Real Speech (pyannote_sample.wav)",
            audio[:64000],  # First 2s of speech (seconds 7-9)
            expected_speech=True
        ))
    except FileNotFoundError:
        print("  Skipped - test audio file not found")
    except Exception as e:
        print(f"  Skipped - error loading audio: {e}")

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)

    passed = sum(1 for r in results if r.passed)
    failed = sum(1 for r in results if not r.passed)

    for r in results:
        status = "PASS" if r.passed else "FAIL"
        expected = "speech" if r.expected_speech else "silence"
        detected = "speech" if r.detected_speech else "silence"
        print(f"  [{status}] {r.name}: expected {expected}, got {detected} (conf: {r.avg_confidence:.3f})")

    print(f"\nResults: {passed}/{len(results)} passed, {failed} failed")

    if failed > 0:
        print("\n*** SOME TESTS FAILED ***")
        print("This indicates the VAD may not properly distinguish speech from noise.")
    else:
        print("\n*** ALL TESTS PASSED ***")
        print("Silero VAD correctly distinguishes speech from background noise.")

    return results


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Test Silero VAD with various audio scenarios")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    args = parser.parse_args()

    results = run_all_tests(verbose=args.verbose)

    # Exit with error if any tests failed
    failed = sum(1 for r in results if not r.passed)
    sys.exit(1 if failed > 0 else 0)


if __name__ == "__main__":
    main()
