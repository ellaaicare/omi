"""
Query caching utilities for database operations.

This module provides decorators and utilities for caching database query results
to improve performance and reduce database load. Uses Redis for distributed caching.
"""

import functools
import hashlib
import json
import os
from typing import Any, Callable, Optional

import redis

from utils.logging_config import get_logger

logger = get_logger(__name__)


def get_cache_client() -> redis.Redis:
    """
    Get a Redis client instance for caching.

    Returns:
        A configured Redis client

    Raises:
        redis.ConnectionError: If connection to Redis fails
    """
    redis_url = os.getenv("REDIS_DB_URI", "redis://localhost:6379/0")

    try:
        client = redis.from_url(redis_url, decode_responses=True)
        # Test connection
        client.ping()
        return client
    except redis.ConnectionError as e:
        logger.error("cache_redis_connection_failed", error=str(e), redis_url=redis_url)
        raise


def generate_cache_key(prefix: str, *args, **kwargs) -> str:
    """
    Generate a unique cache key based on function arguments.

    Args:
        prefix: The prefix for the cache key (typically function name)
        *args: Positional arguments to include in the key
        **kwargs: Keyword arguments to include in the key

    Returns:
        A unique cache key string
    """
    # Create a stable representation of args and kwargs
    key_data = {
        "args": args,
        "kwargs": sorted(kwargs.items()),
    }

    # Serialize and hash
    serialized = json.dumps(key_data, sort_keys=True, default=str)
    hash_suffix = hashlib.md5(serialized.encode()).hexdigest()[:12]

    return f"{prefix}:{hash_suffix}"


def cached_query(
    ttl: int = 300,
    key_prefix: Optional[str] = None,
    serialize: Callable[[Any], str] = json.dumps,
    deserialize: Callable[[str], Any] = json.loads,
):
    """
    Decorator for caching database query results.

    Caches the result of a function in Redis with a specified TTL (time-to-live).
    Useful for expensive database queries that don't change frequently.

    Args:
        ttl: Time-to-live in seconds (default: 300 = 5 minutes)
        key_prefix: Custom prefix for cache keys (default: function name)
        serialize: Function to serialize the result (default: json.dumps)
        deserialize: Function to deserialize cached data (default: json.loads)

    Returns:
        Decorated function with caching behavior

    Example:
        >>> @cached_query(ttl=600)
        ... def get_user_conversations(uid: str, limit: int = 10):
        ...     return db.query(uid, limit)

        >>> # First call hits the database
        >>> conversations = get_user_conversations("user123", limit=20)
        >>> # Subsequent calls within 10 minutes use cache
        >>> conversations = get_user_conversations("user123", limit=20)
    """

    def decorator(func: Callable) -> Callable:
        # Use function name as default prefix
        prefix = key_prefix or f"query_cache:{func.__module__}.{func.__name__}"

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # Generate cache key
            cache_key = generate_cache_key(prefix, *args, **kwargs)

            try:
                # Try to get from cache
                cache_client = get_cache_client()
                cached_value = cache_client.get(cache_key)

                if cached_value is not None:
                    logger.debug(
                        "cache_hit",
                        function=func.__name__,
                        cache_key=cache_key,
                    )
                    return deserialize(cached_value)

                logger.debug(
                    "cache_miss",
                    function=func.__name__,
                    cache_key=cache_key,
                )

            except Exception as e:
                logger.warning(
                    "cache_read_error",
                    function=func.__name__,
                    cache_key=cache_key,
                    error=str(e),
                )
                # Fall through to execute function

            # Execute the function
            result = func(*args, **kwargs)

            # Cache the result
            try:
                cache_client = get_cache_client()
                serialized_result = serialize(result)
                cache_client.setex(cache_key, ttl, serialized_result)

                logger.debug(
                    "cache_write",
                    function=func.__name__,
                    cache_key=cache_key,
                    ttl=ttl,
                )

            except Exception as e:
                logger.warning(
                    "cache_write_error",
                    function=func.__name__,
                    cache_key=cache_key,
                    error=str(e),
                )
                # Don't fail the request if caching fails

            return result

        # Add method to invalidate cache
        def invalidate_cache(*args, **kwargs):
            """Invalidate the cache for specific arguments."""
            cache_key = generate_cache_key(prefix, *args, **kwargs)
            try:
                cache_client = get_cache_client()
                deleted = cache_client.delete(cache_key)

                logger.debug(
                    "cache_invalidated",
                    function=func.__name__,
                    cache_key=cache_key,
                    deleted=deleted,
                )

                return deleted > 0

            except Exception as e:
                logger.error(
                    "cache_invalidation_error",
                    function=func.__name__,
                    cache_key=cache_key,
                    error=str(e),
                )
                return False

        # Add method to invalidate all cache entries for this function
        def invalidate_all_cache():
            """Invalidate all cache entries for this function."""
            pattern = f"{prefix}:*"
            try:
                cache_client = get_cache_client()
                keys = cache_client.keys(pattern)

                if keys:
                    deleted = cache_client.delete(*keys)
                    logger.info(
                        "cache_bulk_invalidated",
                        function=func.__name__,
                        pattern=pattern,
                        deleted=deleted,
                    )
                    return deleted
                return 0

            except Exception as e:
                logger.error(
                    "cache_bulk_invalidation_error",
                    function=func.__name__,
                    pattern=pattern,
                    error=str(e),
                )
                return 0

        # Attach helper methods to the wrapper
        wrapper.invalidate_cache = invalidate_cache
        wrapper.invalidate_all_cache = invalidate_all_cache

        return wrapper

    return decorator


def invalidate_conversation_cache(uid: str, conversation_id: str) -> None:
    """
    Invalidate all cache entries related to a specific conversation.

    Args:
        uid: The user ID
        conversation_id: The conversation ID

    Example:
        >>> # After updating a conversation
        >>> invalidate_conversation_cache(uid, conversation_id)
    """
    try:
        cache_client = get_cache_client()
        patterns = [
            f"query_cache:*:*{uid}*",
            f"query_cache:*:*{conversation_id}*",
        ]

        total_deleted = 0
        for pattern in patterns:
            keys = cache_client.keys(pattern)
            if keys:
                deleted = cache_client.delete(*keys)
                total_deleted += deleted

        logger.debug(
            "conversation_cache_invalidated",
            uid=uid,
            conversation_id=conversation_id,
            deleted=total_deleted,
        )

    except Exception as e:
        logger.error(
            "conversation_cache_invalidation_error",
            uid=uid,
            conversation_id=conversation_id,
            error=str(e),
        )


def invalidate_user_cache(uid: str) -> None:
    """
    Invalidate all cache entries related to a specific user.

    Args:
        uid: The user ID

    Example:
        >>> # After updating user data
        >>> invalidate_user_cache(uid)
    """
    try:
        cache_client = get_cache_client()
        pattern = f"query_cache:*:*{uid}*"
        keys = cache_client.keys(pattern)

        if keys:
            deleted = cache_client.delete(*keys)
            logger.debug(
                "user_cache_invalidated",
                uid=uid,
                deleted=deleted,
            )
        else:
            logger.debug(
                "user_cache_invalidation_no_keys",
                uid=uid,
            )

    except Exception as e:
        logger.error(
            "user_cache_invalidation_error",
            uid=uid,
            error=str(e),
        )
