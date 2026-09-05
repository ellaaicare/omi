"""Periodic bounded deletion for expired Ella diagnostic evidence."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional, Protocol


class DiagnosticRetentionRepository(Protocol):
    async def delete_expired_events(self, *, batch_size: int = 1_000) -> int: ...


@dataclass(frozen=True)
class DiagnosticRetentionRun:
    deleted: int
    backlog_may_remain: bool


class DiagnosticRetentionWorker:
    def __init__(
        self,
        repository: DiagnosticRetentionRepository,
        *,
        interval_seconds: float = 6 * 60 * 60,
        saturated_retry_seconds: float = 30,
        batch_size: int = 1_000,
        max_batches_per_run: int = 20,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("diagnostic retention interval must be positive")
        if saturated_retry_seconds <= 0 or saturated_retry_seconds > interval_seconds:
            raise ValueError("diagnostic saturated retry must be positive and no greater than the interval")
        if batch_size < 1 or batch_size > 10_000:
            raise ValueError("diagnostic retention batch size must be between 1 and 10000")
        if max_batches_per_run < 1 or max_batches_per_run > 100:
            raise ValueError("diagnostic retention batch count must be between 1 and 100")
        self.repository = repository
        self.interval_seconds = interval_seconds
        self.saturated_retry_seconds = saturated_retry_seconds
        self.batch_size = batch_size
        self.max_batches_per_run = max_batches_per_run

    async def run_once(self) -> DiagnosticRetentionRun:
        deleted = 0
        for _ in range(self.max_batches_per_run):
            batch_deleted = await self.repository.delete_expired_events(
                batch_size=self.batch_size,
            )
            deleted += batch_deleted
            if batch_deleted < self.batch_size:
                return DiagnosticRetentionRun(deleted=deleted, backlog_may_remain=False)
        return DiagnosticRetentionRun(deleted=deleted, backlog_may_remain=True)

    async def run_forever(self) -> None:
        while True:
            delay_seconds = self.interval_seconds
            try:
                result = await self.run_once()
                if result.backlog_may_remain:
                    delay_seconds = self.saturated_retry_seconds
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(
                    "[ELLA_DIAGNOSTIC_RETENTION] cleanup failed " f"type={type(exc).__name__}",
                    flush=True,
                )
            await asyncio.sleep(delay_seconds)


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
