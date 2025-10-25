# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with the Omi FastAPI backend.

## Overview

The Omi backend is a sophisticated FastAPI-based serverless application deployed on Modal.com. It provides real-time audio transcription, AI-powered conversation analysis, memory management, and an extensible plugin system. The architecture emphasizes async processing, multi-provider integrations, and enterprise-grade security.

## Architecture

### FastAPI Structure
```python
# main.py - Central FastAPI application with 16 routers
app = FastAPI()

# Core routers
app.include_router(transcribe.router)    # WebSocket transcription
app.include_router(conversations.router)  # Conversation CRUD
app.include_router(memories.router)      # Memory management
app.include_router(chat.router)          # AI chat interface
app.include_router(apps.router)          # Plugin system
# ... 11 additional routers
```

### Modal.com Deployment
```python
# Serverless configuration
modal_app = App(
    name='backend',
    secrets=[Secret.from_name("gcp-credentials"), Secret.from_name('envs')],
)
image = Image.debian_slim().apt_install('ffmpeg', 'git', 'unzip')
    .pip_install_from_requirements('requirements.txt')

@modal_app.function(
    image=image,
    memory=(512, 1024),  # 512MB-1GB dynamic scaling
    cpu=2,
    allow_concurrent_inputs=10,
    timeout=60 * 10,
)
```

### Database Architecture
- **Primary**: Google Cloud Firestore (document NoSQL)
- **Caching**: Redis (Upstash) for real-time features
- **Vector**: Pinecone for semantic search and embeddings
- **Encryption**: Transparent encryption layer for sensitive data

## Development Commands

### Local Development Setup
```bash
# Environment setup
cp .env.template .env
python -m venv venv
source venv/bin/activate  # Linux/macOS
# venv\Scripts\activate   # Windows

# Install dependencies
pip install -r requirements.txt

# Configure required services (see .env.template):
# - Firebase project with Firestore
# - Redis instance (Upstash recommended)
# - OpenAI API key
# - Deepgram API key
# - Pinecone index

# Start development server
uvicorn main:app --reload --env-file .env
```

### Modal.com Deployment
```bash
# Deploy to production
modal deploy main.py

# Development deployment with hot reload
modal serve main.py

# View logs
modal logs backend

# Check app status
modal app list
```

### ngrok for Webhook Testing
```bash
# Get ngrok static domain and run:
ngrok http --domain=your-domain.ngrok-free.app 8000

# Update Flutter app's BASE_API_URL to ngrok URL
```

### Testing
```bash
# Run tests (when test suite is available)
pytest
pytest tests/transcription/
pytest tests/auth/

# Integration testing with real services
pytest tests/integration/
```

## Core Services and APIs

### Audio Transcription (WebSocket)
**Router**: `routers/transcribe.py`

Multi-provider real-time transcription service:

```python
# WebSocket endpoint
@router.websocket("/transcribe-speech")
async def transcribe_speech_endpoint(
    websocket: WebSocket,
    language: str = 'en',
    codec: str = 'opus',
    channels: int = 1,
    include_speech_profile: bool = False,
    stt_service: str = 'deepgram',
)

# Supported STT providers
PROVIDERS = {
    'deepgram': DeepgramTranscription,
    'soniox': SonioxTranscription,
    'speechmatics': SpeechmaticsTranscription
}
```

**Key Features**:
- Real-time WebSocket streaming
- Multiple codec support (Opus, PCM, mulaw)
- Voice Activity Detection (VAD)
- Speaker diarization with pyannote.audio
- Language auto-detection and routing

### Conversation Management
**Router**: `routers/conversations.py`

```python
# Core conversation endpoints
POST   /conversations               # Create conversation
GET    /conversations               # List conversations
GET    /conversations/{id}          # Get conversation details
DELETE /conversations/{id}          # Delete conversation
PATCH  /conversations/{id}/segments # Update segments
```

**Features**:
- Automatic conversation structuring
- Speaker identification and labeling
- Real-time conversation updates
- Conversation search and filtering

### Memory System
**Router**: `routers/memories.py`

AI-powered memory creation and management:

```python
# Memory endpoints
GET    /memories                    # List memories with filtering
POST   /memories                    # Create memory manually
DELETE /memories/{id}               # Delete memory
POST   /memories/{id}/events        # Add events to memory
```

**Memory Processing Pipeline**:
1. Conversation analysis with LLM
2. Memory extraction and categorization
3. Vector embedding generation
4. Pinecone storage for semantic search

### Plugin/App System
**Router**: `routers/apps.py`

Extensible plugin architecture:

```python
# App management
GET    /apps                        # List available apps
POST   /apps                        # Create custom app
GET    /apps/{id}                   # Get app details
POST   /apps/{id}/webhook           # Trigger app webhook
POST   /apps/{id}/oauth/authorize   # OAuth flow
```

**Plugin Categories** (16 supported):
- conversation, transcription, chat
- health, fitness, sleep
- education, productivity, finance
- entertainment, travel, social, etc.

### AI Chat Interface
**Router**: `routers/chat.py`

Multi-modal chat with voice support:

```python
# Chat endpoints
POST   /chat                        # Send chat message
POST   /chat/voice                  # Voice message chat
GET    /chat/history                # Chat history
```

**Features**:
- Multiple LLM providers (OpenAI, Groq)
- Voice message transcription and response
- Conversation context awareness
- Streaming responses

## Database Patterns

### Firestore Integration
```python
# database/_client.py - Centralized client
from google.cloud import firestore

db = firestore.Client()

# Common patterns
def get_user_data(user_id: str):
    doc = db.collection('users').document(user_id).get()
    return doc.to_dict() if doc.exists else None

def save_conversation(user_id: str, conversation_data: dict):
    db.collection('users').document(user_id)
      .collection('conversations').add(conversation_data)
```

### Encryption Layer
```python
# Automatic encryption for sensitive data
from utils.other.crypt import encrypt_string, decrypt_string

# Conversations and memories are automatically encrypted
encrypted_data = encrypt_string(sensitive_content)
decrypted_data = decrypt_string(encrypted_data)
```

### Redis Caching
```python
# utils/redis_client.py
import redis
redis_db = redis.Redis.from_url(REDIS_URL)

# Common caching patterns
def cache_user_session(user_id: str, data: dict):
    redis_db.setex(f"session:{user_id}", 3600, json.dumps(data))
```

## Authentication & Authorization

### Firebase Authentication
```python
# dependencies.py - Auth dependency
from firebase_admin import auth

async def get_current_user_uid(authorization: str = Header()):
    token = authorization.replace("Bearer ", "")
    decoded_token = auth.verify_id_token(token)
    return decoded_token['uid']
```

### API Key Authentication
```python
# For MCP (Model Control Protocol) integration
async def get_mcp_user(api_key: str = Header(alias="x-api-key")):
    if not api_key or api_key != MCP_API_KEY:
        raise HTTPException(401, "Invalid API key")
    return api_key
```

## External Service Integrations

### AI/ML Services
```python
# OpenAI integration
from openai import OpenAI
client = OpenAI(api_key=OPENAI_API_KEY)

# LangChain for complex workflows
from langchain.chains import ConversationChain
from langchain.llms import OpenAI

# Groq for high-speed inference
from groq import Groq
groq_client = Groq(api_key=GROQ_API_KEY)
```

### Vector Database (Pinecone)
```python
# Pinecone for semantic search
import pinecone
from pinecone import Pinecone

pc = Pinecone(api_key=PINECONE_API_KEY)
index = pc.Index(PINECONE_INDEX_NAME)

# Memory search
def search_memories(user_id: str, query: str, top_k: int = 10):
    # Generate embedding for query
    embedding = get_embedding(query)

    # Search Pinecone
    results = index.query(
        vector=embedding,
        filter={"user_id": user_id},
        top_k=top_k,
        include_metadata=True
    )
    return results.matches
```

### Payment Integration (Stripe)
```python
# routers/payment.py
import stripe
stripe.api_key = STRIPE_SECRET_KEY

# Subscription management
@router.post("/create-checkout-session")
async def create_checkout_session(user_data):
    session = stripe.checkout.Session.create(
        payment_method_types=['card'],
        line_items=[{
            'price': price_id,
            'quantity': 1,
        }],
        mode='subscription',
        # ... configuration
    )
```

## Real-time Features

### WebSocket Integration
```python
# Real-time transcription WebSocket
@router.websocket("/transcribe-speech")
async def transcribe_speech_endpoint(websocket: WebSocket):
    await websocket.accept()

    # Audio streaming loop
    while True:
        audio_data = await websocket.receive_bytes()
        # Process audio with STT provider
        transcript = await transcribe_audio_chunk(audio_data)
        await websocket.send_json({
            'transcript': transcript,
            'is_final': True
        })
```

### Background Processing
```python
# utils/other/utils.py - Safe task creation
import asyncio

def create_safe_task(coro):
    """Create asyncio task with error handling"""
    task = asyncio.create_task(coro)
    task.add_done_callback(lambda t: t.exception() if t.exception() else None)
    return task

# Usage for background processing
create_safe_task(process_conversation_memory(conversation_id))
```

## Common Development Tasks

### Adding New Router
1. Create router file in `routers/`
2. Import in `main.py`
3. Add to `app.include_router()`
4. Follow existing auth patterns

### Adding STT Provider
1. Create provider class in `utils/stt/`
2. Implement required interface methods
3. Add to provider dictionary in `streaming.py`
4. Test with WebSocket endpoint

### Adding AI Model Integration
1. Add API credentials to `.env.template`
2. Create service wrapper in `utils/llm/`
3. Integrate with conversation/memory processing
4. Add error handling and fallbacks

### Database Schema Changes
1. Update models in `models/` directory
2. Consider encryption requirements
3. Plan migration strategy for existing data
4. Update API serialization/deserialization

## Environment Variables

### Required Services
```bash
# AI Services
OPENAI_API_KEY=sk-...
DEEPGRAM_API_KEY=...
GROQ_API_KEY=gsk_...

# Google Cloud / Firebase
GOOGLE_APPLICATION_CREDENTIALS=path/to/service-account.json
FIREBASE_CONFIG={"project_id": "..."}

# Database
REDIS_URL=redis://...
PINECONE_API_KEY=...
PINECONE_INDEX_NAME=omi-memories

# External Services
STRIPE_SECRET_KEY=sk_test_...
STRIPE_PUBLISHABLE_KEY=pk_test_...

# Authentication
MCP_API_KEY=your-secure-key
ADMIN_KEY=admin-access-key
```

## Common Issues & Debugging

### Audio Processing
- **No Audio**: Check WebSocket connection and codec compatibility
- **Poor Transcription**: Verify STT provider credentials and audio quality
- **Latency Issues**: Optimize VAD settings and chunk sizes

### Database Connectivity
- **Firestore Errors**: Verify Google Cloud credentials and project setup
- **Redis Connection**: Check Redis URL format and network access
- **Pinecone Issues**: Verify index exists and API key permissions

### Authentication
- **JWT Errors**: Check Firebase project configuration
- **Token Expiration**: Implement proper token refresh logic
- **Permission Denied**: Verify user roles and Firestore security rules

### Deployment
- **Modal.com Issues**: Check secrets configuration and resource limits
- **Cold Starts**: Consider keep-warm settings for critical endpoints
- **Memory Limits**: Monitor usage and adjust container specifications

## Performance Considerations

### Optimization Strategies
- Use Redis caching for frequently accessed data
- Implement pagination for large datasets
- Use background tasks for heavy processing
- Optimize Pinecone queries with proper filtering

### Monitoring
- Modal.com provides built-in monitoring
- Use structured logging for debugging
- Monitor API response times and error rates
- Track resource usage and costs

## Security Best Practices

- All sensitive data encrypted at rest
- Firebase security rules for data access control
- API key rotation and secure storage
- Input validation and sanitization
- Rate limiting on expensive endpoints
- CORS configuration for web clients

This backend represents a production-ready, scalable system designed for real-time AI-powered conversation processing with enterprise-grade security and reliability.