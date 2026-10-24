"""Durable privacy-first bridge, called before existing Firestore deletion.

No new background job or distributed atomicity claim. If Firestore subsequently
fails, the source remains withdrawn; repeating normal deletion is idempotent.
"""
import asyncio


async def retire_muse_conversations(uid, conversation_ids, *, pool_factory=None):
    if (not isinstance(uid, str) or not uid or not isinstance(conversation_ids, list)
            or not 1 <= len(conversation_ids) <= 450
            or any(not isinstance(cid, str) or not 1 <= len(cid) <= 256 for cid in conversation_ids)
            or len(set(conversation_ids)) != len(conversation_ids)):
        raise ValueError('muse_retirement_invalid')
    if pool_factory is None:
        from database.ella_postgres import create_dedicated_ella_postgres_pool
        pool_factory = create_dedicated_ella_postgres_pool
    pool = None
    try:
        async with asyncio.timeout(10):
            pool = await pool_factory(min_size=1, max_size=1)
            async with pool.acquire(timeout=3) as connection:
                async with connection.transaction():
                    await connection.execute("SET LOCAL statement_timeout='3000ms'")
                    await connection.execute("SET LOCAL lock_timeout='2000ms'")
                    for cid in conversation_ids:
                        await connection.fetchval('SELECT ella_muse_withdraw_conversation($1,$2)', uid, cid, timeout=3)
    finally:
        if pool is not None:
            try:
                await asyncio.wait_for(pool.close(), 2)
            except BaseException:
                pool.terminate()
                raise


def retire_muse_conversations_sync(uid, conversation_ids):
    # Existing callers run inside a threadpool/synchronous database boundary.
    # Refuse accidental invocation on the application event loop.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError('muse_retirement_requires_sync_boundary')
    try:
        return asyncio.run(retire_muse_conversations(uid, conversation_ids))
    except Exception:
        from utils.ella.memory_artwork_storage import MemoryArtworkStorageError
        raise MemoryArtworkStorageError('muse_source_retirement_unavailable') from None
