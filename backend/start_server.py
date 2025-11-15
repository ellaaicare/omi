#!/usr/bin/env python3
"""
Simple script to start the OMI backend server with proper environment loading
"""
import os
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# Ensure GOOGLE_APPLICATION_CREDENTIALS is set to absolute path
if not os.environ.get('GOOGLE_APPLICATION_CREDENTIALS'):
    os.environ['GOOGLE_APPLICATION_CREDENTIALS'] = os.path.join(
        os.path.dirname(__file__),
        'google-credentials.json'
    )

# Set Opus library path for macOS Homebrew
opus_lib_path = '/opt/homebrew/opt/opus/lib'
if os.path.exists(opus_lib_path):
    current_dyld = os.environ.get('DYLD_LIBRARY_PATH', '')
    if current_dyld:
        os.environ['DYLD_LIBRARY_PATH'] = f"{opus_lib_path}:{current_dyld}"
    else:
        os.environ['DYLD_LIBRARY_PATH'] = opus_lib_path

# Set GitHub token for torch.hub downloads
github_token = os.environ.get('GITHUB_TOKEN')
if github_token:
    # torch.hub will use this when downloading from GitHub
    os.environ['GITHUB_TOKEN'] = github_token

# Set SSL certificate file for macOS Python
try:
    import certifi
    os.environ['SSL_CERT_FILE'] = certifi.where()
    os.environ['REQUESTS_CA_BUNDLE'] = certifi.where()
except ImportError:
    pass

# Start uvicorn
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=True
    )
