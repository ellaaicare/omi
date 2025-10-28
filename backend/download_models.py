#!/usr/bin/env python3
"""
Download all ML models for OMI backend
This will download PyAnnote speaker diarization models (~500MB-1GB)
"""
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

print("🚀 Downloading PyAnnote speaker diarization models...")
print("This will download ~500MB-1GB of models")
print("Models will be cached for future use\n")

# Import PyAnnote Pipeline (this triggers model download)
try:
    from pyannote.audio import Pipeline

    # You need a Hugging Face token for PyAnnote models
    hf_token = os.getenv('HUGGINGFACE_TOKEN')
    if not hf_token:
        print("❌ ERROR: HUGGINGFACE_TOKEN not found in .env file")
        print("Please add your Hugging Face token to .env")
        exit(1)

    print("✅ Hugging Face token found")
    print("📥 Downloading speaker diarization pipeline...")

    # This will download the models
    pipeline = Pipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        use_auth_token=hf_token
    )

    print("\n✅ PyAnnote models downloaded successfully!")
    print(f"📍 Models cached in: ~/.cache/huggingface/")

    # Check cache size
    import subprocess
    result = subprocess.run(
        ["du", "-sh", os.path.expanduser("~/.cache/huggingface/")],
        capture_output=True,
        text=True
    )
    if result.returncode == 0:
        print(f"💾 Cache size: {result.stdout.strip()}")

except Exception as e:
    print(f"❌ Error downloading models: {e}")
    print("\nTroubleshooting:")
    print("1. Make sure HUGGINGFACE_TOKEN is set in .env")
    print("2. Accept model license at: https://huggingface.co/pyannote/speaker-diarization-3.1")
    exit(1)

print("\n✅ All PyAnnote models ready!")
