# PRD: Backend Performance & Refactoring

## Status
🟡 **HIGH PRIORITY** - Performance improvements and code quality

## Executive Summary
Refactor large, complex backend modules (especially `transcribe.py`) to improve maintainability, add distributed locking, and optimize performance through better caching and query optimization.

## Problem Statement
- **transcribe.py:** 1,127-line function with nested async functions
- **Missing distributed locking:** Race conditions in conversation processing
- **Silent error swallowing:** 40+ functions with try-catch decorator returning None
- **Blocking operations in async context:** Synchronous DB calls in async functions
- **200+ print statements:** No structured logging

## Goals
- Refactor `transcribe.py` into maintainable classes
- Implement distributed locking for conversation processing
- Add structured logging
- Optimize database queries with caching
- Standardize error handling

## Success Metrics
- ✅ No functions >200 lines
- ✅ Distributed locking prevents race conditions
- ✅ Structured JSON logging in production
- ✅ 50% reduction in database queries (via caching)
- ✅ Zero silent error swallowing

## Technical Specification

### 1. Refactor transcribe.py

**Current Structure (1,227 lines):**
```python
async def _listen(...):  # 1,127 lines
    # 15+ nested async functions
    async def _record_usage_periodically(): ...
    async def _asend_message_event(): ...
    async def send_heartbeat(): ...
    # ... 12 more nested functions
```

**Target Structure:**
```python
# backend/services/transcription/session_manager.py
class TranscriptionSession:
    """Manages a single WebSocket transcription session."""

    def __init__(self, websocket, uid, config):
        self.websocket = websocket
        self.uid = uid
        self.config = config
        self.conversation_manager = ConversationManager(uid)
        self.audio_processor = AudioProcessor(config)
        self.usage_tracker = UsageTracker(uid)

    async def start(self):
        """Start transcription session."""
        await self._setup_streams()
        await self._start_heartbeat()
        await asyncio.gather(
            self._process_audio_stream(),
            self._process_transcripts(),
            self._send_events(),
        )

    async def stop(self):
        """Clean up session resources."""
        await self._cleanup_streams()
        await self.conversation_manager.finalize()

# backend/services/transcription/audio_processor.py
class AudioProcessor:
    """Processes incoming audio chunks."""

    async def process_chunk(self, audio_data: bytes) -> AudioChunk:
        """Process single audio chunk."""
        # VAD, codec handling, buffering
        pass

# backend/services/transcription/conversation_manager.py
class ConversationManager:
    """Manages conversation lifecycle and locking."""

    async def acquire_lock(self, conversation_id: str) -> bool:
        """Acquire distributed lock for conversation."""
        return await self.redis_lock.acquire(
            f"conversation:{conversation_id}",
            timeout=300  # 5 minutes
        )

    async def finalize(self):
        """Finalize conversation processing."""
        async with self.lock:
            await self._process_segments()
            await self._trigger_integrations()
            await self._release_lock()
```

**Benefits:**
- Testable components
- Clear responsibilities
- Reusable across endpoints
- Easier to debug

### 2. Implement Distributed Locking

**Add Redis Lock Library:**
```python
# requirements.txt
redis-lock==4.0.0

# backend/utils/locking.py
from redis import Redis
from redis_lock import Lock as RedisLock
import structlog

logger = structlog.get_logger()

class DistributedLock:
    """Distributed lock using Redis."""

    def __init__(self, redis_client: Redis):
        self.redis = redis_client

    async def acquire(
        self,
        resource: str,
        timeout: int = 300,
        blocking: bool = True
    ) -> RedisLock:
        """Acquire lock on resource."""
        lock = RedisLock(
            self.redis,
            resource,
            expire=timeout,
            auto_renewal=True
        )

        if blocking:
            acquired = lock.acquire(blocking=True, timeout=timeout)
        else:
            acquired = lock.acquire(blocking=False)

        if not acquired:
            logger.warning(f"Failed to acquire lock: {resource}")
            raise LockAcquisitionError(f"Could not acquire lock: {resource}")

        logger.info(f"Acquired lock: {resource}")
        return lock

# Usage in conversation processing:
class ConversationManager:
    async def process_conversation(self, conversation_id: str):
        lock = await self.distributed_lock.acquire(
            f"conversation:{self.uid}:{conversation_id}"
        )

        try:
            # Process conversation - only one worker at a time
            conversation = await self.db.get_conversation(...)
            result = await self.llm.process(conversation)
            await self.db.update_conversation(...)
        finally:
            lock.release()
```

### 3. Add Structured Logging

**Replace print() with structlog:**
```python
# requirements.txt
structlog==23.1.0

# backend/utils/logging_config.py
import structlog
import logging
import sys

def configure_logging(environment: str = "production"):
    """Configure structured logging."""

    processors = [
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
    ]

    if environment == "production":
        processors.append(structlog.processors.JSONRenderer())
    else:
        processors.append(structlog.dev.ConsoleRenderer())

    structlog.configure(
        processors=processors,
        wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=True,
    )

# Usage in code:
import structlog

logger = structlog.get_logger()

# Instead of: print(f"Processing conversation {conv_id}")
logger.info("processing_conversation", conversation_id=conv_id, uid=uid)

# Instead of: print(f"Error: {e}")
logger.error("conversation_processing_failed", error=str(e), conversation_id=conv_id)
```

### 4. Optimize Database Queries

**Add Query Caching:**
```python
# backend/database/cached_queries.py
from functools import wraps
import json

def cached_query(ttl: int = 300):
    """Cache database query results in Redis."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = f"query:{func.__name__}:{hash(str(args) + str(kwargs))}"

            # Try cache first
            cached = r.get(cache_key)
            if cached:
                return json.loads(cached)

            # Query database
            result = await func(*args, **kwargs)

            # Cache result
            r.setex(cache_key, ttl, json.dumps(result))

            return result

        return wrapper
    return decorator

# Usage:
@cached_query(ttl=600)  # Cache for 10 minutes
def get_conversation(uid: str, conversation_id: str):
    user_ref = db.collection('users').document(uid)
    conversation_ref = user_ref.collection('conversations').document(conversation_id)
    return conversation_ref.get().to_dict()
```

### 5. Standardize Error Handling

**Replace try_catch_decorator:**
```python
# backend/utils/error_handling.py
from enum import Enum
from typing import Optional
import structlog

logger = structlog.get_logger()

class ErrorSeverity(Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

def safe_operation(
    default_value: Optional[any] = None,
    severity: ErrorSeverity = ErrorSeverity.MEDIUM,
    reraise: bool = False
):
    """Safe operation wrapper with proper logging."""

    def decorator(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            try:
                return await func(*args, **kwargs)
            except Exception as e:
                logger.error(
                    "operation_failed",
                    function=func.__name__,
                    error=str(e),
                    severity=severity.value,
                    args=str(args)[:100],  # Truncate for logging
                )

                if severity == ErrorSeverity.CRITICAL:
                    # Alert monitoring system
                    await alert_monitoring(func.__name__, e)

                if reraise:
                    raise

                return default_value

        return wrapper
    return decorator

# Usage:
@safe_operation(default_value=0, severity=ErrorSeverity.LOW)
async def get_usage_count(app_id: str) -> int:
    count = await redis.get(f"usage:{app_id}")
    return int(count)
```

## Implementation Plan

### Week 1-2: Refactor transcribe.py
- [ ] Create session_manager.py
- [ ] Create audio_processor.py
- [ ] Create conversation_manager.py
- [ ] Migrate nested functions to classes
- [ ] Add tests for each component

### Week 3: Distributed Locking & Logging
- [ ] Add redis-lock library
- [ ] Implement DistributedLock class
- [ ] Add to conversation processing
- [ ] Replace print() with structlog
- [ ] Configure JSON logging for production

### Week 4: Performance Optimization
- [ ] Add query caching
- [ ] Optimize N+1 queries
- [ ] Replace try_catch_decorator
- [ ] Performance testing

## Success Criteria

- [ ] transcribe.py split into 4+ files
- [ ] No function >200 lines
- [ ] Distributed locking prevents races
- [ ] All print() replaced with logger
- [ ] 50% query reduction via caching

---

**Estimated Effort:** 4 weeks
**Priority:** 🟡 HIGH
**Target Completion:** 4-5 weeks
