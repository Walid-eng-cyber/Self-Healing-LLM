"""
Deferrable work queue (Phase 3).

Requests are classified at the API boundary as interactive or deferrable:

  * interactive  - the caller is waiting, so it fails fast: if the request
                   can't be served now, the caller gets an error immediately.
  * deferrable   - the caller does not need the answer right now, so it can
                   survive an outage: if the request can't be served now, it is
                   queued and retried in the background with exponential backoff
                   until a provider recovers (or attempts are exhausted).

This module implements the queue and its background worker. The queue is
in-process (an asyncio.Queue plus a dict of job records); a durable, restart-safe
backend (e.g. Redis) can replace it behind the same interface later.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Callable

from .metadata import RequestMetadata
from .providers import ProviderError, RouterProvider
from .schemas import ChatCompletionRequest, ChatCompletionResponse


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    request: ChatCompletionRequest
    request_class: str
    meta: RequestMetadata
    status: JobStatus = JobStatus.QUEUED
    attempts: int = 0
    error: str | None = None
    result: ChatCompletionResponse | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class JobQueue:
    def __init__(
        self,
        router: RouterProvider,
        *,
        max_attempts: int = 10,
        backoff_base_s: float = 1.0,
        backoff_cap_s: float = 30.0,
        on_success: Callable[[Job], None] | None = None,
        sleep: Callable[[float], "asyncio.Future"] = asyncio.sleep,
    ) -> None:
        self._router = router
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._on_success = on_success
        self._sleep = sleep

        # The asyncio.Queue is created in start(), inside the running event loop,
        # so it binds to the right loop (important when the app is restarted or
        # tested across multiple loops).
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._jobs: dict[str, Job] = {}
        self._worker: asyncio.Task | None = None

    # -- public API ---------------------------------------------------------

    def submit(
        self,
        request: ChatCompletionRequest,
        request_class: str,
        meta: RequestMetadata,
    ) -> Job:
        """Enqueue a deferrable request and return its Job record."""
        job = Job(
            id=f"job-{uuid.uuid4().hex}",
            request=request,
            request_class=request_class,
            meta=meta,
        )
        self._jobs[job.id] = job
        self._queue.put_nowait(job.id)
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def start(self) -> None:
        if self._worker is None:
            # Bind the queue to the currently running loop.
            self._queue = asyncio.Queue()
            self._worker = asyncio.create_task(self._run())

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

    # -- worker -------------------------------------------------------------

    def _backoff(self, attempts: int) -> float:
        return min(self.backoff_cap_s, self.backoff_base_s * (2 ** (attempts - 1)))

    async def _run(self) -> None:
        while True:
            job_id = await self._queue.get()
            try:
                await self._process(self._jobs[job_id])
            finally:
                self._queue.task_done()

    async def _process(self, job: Job) -> None:
        job.status = JobStatus.RUNNING
        while True:
            job.attempts += 1
            job.updated_at = time.time()
            try:
                result = await self._router.complete(
                    job.request, request_class=job.request_class
                )
            except ProviderError as exc:
                job.error = str(exc)
                if job.attempts >= self.max_attempts:
                    job.status = JobStatus.FAILED
                    job.updated_at = time.time()
                    return
                # Providers still down -> wait and retry. This is what lets a
                # deferrable request survive the outage.
                await self._sleep(self._backoff(job.attempts))
                continue
            except Exception as exc:  # pragma: no cover - unexpected
                job.error = f"unexpected: {type(exc).__name__}: {exc}"
                job.status = JobStatus.FAILED
                job.updated_at = time.time()
                return

            job.result = result
            job.error = None
            job.status = JobStatus.SUCCEEDED
            job.updated_at = time.time()
            if self._on_success is not None:
                try:
                    self._on_success(job)
                except Exception:  # pragma: no cover - accounting must not break jobs
                    pass
            return
