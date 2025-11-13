"""
Distributed locking utilities for conversation management.

This module provides distributed locking mechanisms to prevent race conditions
when multiple processes or instances are accessing the same conversation data.
Uses Redis-based distributed locks for thread-safe and process-safe operations.
"""

import os
from contextlib import contextmanager
from typing import Generator, Optional

import redis
from redis_lock import Lock

from utils.logging_config import get_logger

logger = get_logger(__name__)


class LockAcquisitionError(Exception):
    """Raised when a lock cannot be acquired within the timeout period."""

    pass


class LockReleaseError(Exception):
    """Raised when there's an error releasing a lock."""

    pass


def get_redis_client() -> redis.Redis:
    """
    Get a Redis client instance.

    Returns:
        A configured Redis client

    Raises:
        redis.ConnectionError: If connection to Redis fails
    """
    redis_url = os.getenv("REDIS_DB_URI", "redis://localhost:6379/0")

    try:
        client = redis.from_url(redis_url, decode_responses=False)
        # Test connection
        client.ping()
        return client
    except redis.ConnectionError as e:
        logger.error("redis_connection_failed", error=str(e), redis_url=redis_url)
        raise


@contextmanager
def conversation_lock(
    uid: str,
    conversation_id: str,
    timeout: int = 60,
    auto_renewal: bool = True,
    expire: int = 120,
) -> Generator[Lock, None, None]:
    """
    Context manager for acquiring a distributed lock on a conversation.

    This ensures that only one process can modify a conversation at a time,
    preventing race conditions in distributed environments.

    Args:
        uid: The user ID
        conversation_id: The conversation ID to lock
        timeout: Maximum time (seconds) to wait for lock acquisition (default: 60)
        auto_renewal: Whether to automatically renew the lock (default: True)
        expire: Lock expiration time in seconds (default: 120)

    Yields:
        Lock: The acquired lock object

    Raises:
        LockAcquisitionError: If lock cannot be acquired within timeout
        LockReleaseError: If lock cannot be released properly

    Example:
        >>> with conversation_lock(uid, conv_id, timeout=60):
        ...     # Perform conversation updates
        ...     update_conversation_data(conv_id, data)
    """
    redis_client = get_redis_client()
    lock_key = f"conversation_lock:{uid}:{conversation_id}"

    logger.debug(
        "acquiring_conversation_lock",
        uid=uid,
        conversation_id=conversation_id,
        lock_key=lock_key,
        timeout=timeout,
    )

    # Create the lock
    lock = Lock(
        redis_client,
        lock_key,
        expire=expire,
        auto_renewal=auto_renewal,
        id=None,
    )

    try:
        # Try to acquire the lock
        acquired = lock.acquire(blocking=True, timeout=timeout)

        if not acquired:
            logger.error(
                "lock_acquisition_failed",
                uid=uid,
                conversation_id=conversation_id,
                lock_key=lock_key,
                timeout=timeout,
            )
            raise LockAcquisitionError(
                f"Failed to acquire lock for conversation {conversation_id} within {timeout} seconds"
            )

        logger.debug(
            "conversation_lock_acquired",
            uid=uid,
            conversation_id=conversation_id,
            lock_key=lock_key,
        )

        yield lock

    except Exception as e:
        logger.error(
            "lock_operation_error",
            uid=uid,
            conversation_id=conversation_id,
            lock_key=lock_key,
            error=str(e),
            exc_info=True,
        )
        raise

    finally:
        # Release the lock
        try:
            if lock.locked():
                lock.release()
                logger.debug(
                    "conversation_lock_released",
                    uid=uid,
                    conversation_id=conversation_id,
                    lock_key=lock_key,
                )
        except Exception as e:
            logger.error(
                "lock_release_failed",
                uid=uid,
                conversation_id=conversation_id,
                lock_key=lock_key,
                error=str(e),
                exc_info=True,
            )
            raise LockReleaseError(f"Failed to release lock for conversation {conversation_id}") from e


@contextmanager
def user_lock(
    uid: str,
    timeout: int = 30,
    auto_renewal: bool = True,
    expire: int = 60,
) -> Generator[Lock, None, None]:
    """
    Context manager for acquiring a distributed lock on a user.

    This ensures that only one process can perform critical user operations
    at a time.

    Args:
        uid: The user ID to lock
        timeout: Maximum time (seconds) to wait for lock acquisition (default: 30)
        auto_renewal: Whether to automatically renew the lock (default: True)
        expire: Lock expiration time in seconds (default: 60)

    Yields:
        Lock: The acquired lock object

    Raises:
        LockAcquisitionError: If lock cannot be acquired within timeout
        LockReleaseError: If lock cannot be released properly

    Example:
        >>> with user_lock(uid, timeout=30):
        ...     # Perform user-level updates
        ...     update_user_credits(uid)
    """
    redis_client = get_redis_client()
    lock_key = f"user_lock:{uid}"

    logger.debug(
        "acquiring_user_lock",
        uid=uid,
        lock_key=lock_key,
        timeout=timeout,
    )

    # Create the lock
    lock = Lock(
        redis_client,
        lock_key,
        expire=expire,
        auto_renewal=auto_renewal,
        id=None,
    )

    try:
        # Try to acquire the lock
        acquired = lock.acquire(blocking=True, timeout=timeout)

        if not acquired:
            logger.error(
                "user_lock_acquisition_failed",
                uid=uid,
                lock_key=lock_key,
                timeout=timeout,
            )
            raise LockAcquisitionError(f"Failed to acquire user lock for {uid} within {timeout} seconds")

        logger.debug(
            "user_lock_acquired",
            uid=uid,
            lock_key=lock_key,
        )

        yield lock

    except Exception as e:
        logger.error(
            "user_lock_operation_error",
            uid=uid,
            lock_key=lock_key,
            error=str(e),
            exc_info=True,
        )
        raise

    finally:
        # Release the lock
        try:
            if lock.locked():
                lock.release()
                logger.debug(
                    "user_lock_released",
                    uid=uid,
                    lock_key=lock_key,
                )
        except Exception as e:
            logger.error(
                "user_lock_release_failed",
                uid=uid,
                lock_key=lock_key,
                error=str(e),
                exc_info=True,
            )
            raise LockReleaseError(f"Failed to release user lock for {uid}") from e
