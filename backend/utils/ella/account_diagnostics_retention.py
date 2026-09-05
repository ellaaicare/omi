"""Periodic bounded deletion for expired Ella diagnostic evidence."""

from __future__ import annotations

import asyncio
from typing import Optional, Protocol


class DiagnosticRetentionRepository(Protocol):
    async def delete_expired_events(self, *, batch_size: int = 1_000) -> int: ...


class DiagnosticRetentionWorker:
    def __init__(
        self,
        repository: DiagnosticRetentionRepository,
        *,
        interval_seconds: float = 6 * 60 * 60,
        batch_size: int = 1_000,
        max_batches_per_run: int = 20,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("diagnostic retention interval must be positive")
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("diagnostic retention batch size must be between 1 and 10000")
        if max_batches_per_run < 1 or max_batches_per_run > 100:
            raise ValueError("diagnostic retention batch count must be between 1 and 100")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.batch_size = batch_size
        self.max_batches_per_run = max_batches_per_run

    async def run_once(self) -> int:
        deleted = 0
        for _ in range(self.max_batches_per_run):
            batch_deleted = await self.repository.delete_expired_events(
                batch_size=self.batch_size,
            )
            deleted += batch_deleted
            if batch_deleted < self.batch_size:
                break
        return deleted

    async def run_forever(self) -> None:
        while True:
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "[ELLA_DIAGNOSTIC_RETENTION] cleanup failed " f"type={type(exc).__name__}",
                    flush=True,
                )
            await asyncio.sleep(self.interval_seconds)


_worker_task: Optional[asyncio.Task] = None


async def start_diagnostic_retention_worker(worker: DiagnosticRetentionWorker) -> None:
    global _worker_task
    if _worker_task and not _worker_task.done():
        return
    _worker_task = asyncio.create_task(worker.run_forever())


async def stop_diagnostic_retention_worker() -> None:
    global _worker_task
    if not _worker_task:
        return
    _worker_task.cancel()
    try:
        await _worker_task
    except asyncio.CancelledError:
        pass
    _worker_task = None
