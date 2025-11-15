#!/usr/bin/env python3
"""
Download WhisperX models for offline use
This will download Whisper models (~2GB)
"""
import os
import whisperx

print("🚀 Downloading WhisperX Whisper models...")
print("This will download ~2GB of models")
print("Models will be cached for future use\n")

# Download models for different sizes
models_to_download = [
    ("base", "Best balance of speed/accuracy for testing"),
    ("small", "Better accuracy, slower (~500MB)"),
    # ("medium", "High accuracy, slow (~1.5GB)"),  # Optional - uncomment if needed
]

for model_name, description in models_to_download:
    print(f"\n📥 Downloading '{model_name}' model - {description}")

    try:
        # This will download the model if not cached
        model = whisperx.load_model(
            model_name,
            device="cpu",  # Use CPU for now
            compute_type="int8",  # Smaller, faster
            download_root=None  # Use default cache
        )
        print(f"✅ '{model_name}' model downloaded successfully!")
        del model  # Free memory

    except Exception as e:
        print(f"❌ Error downloading '{model_name}' model: {e}")

print("\n✅ WhisperX models ready!")
print(f"📍 Models cached in: ~/.cache/huggingface/ and ~/.cache/torch/")

# Check cache size
import subprocess
result = subprocess.run(
    ["du", "-sh", os.path.expanduser("~/.cache/huggingface/")],
    capture_output=True,
    text=True
)
if result.returncode == 0:
    print(f"💾 Hugging Face cache size: {result.stdout.strip()}")
