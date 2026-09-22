"""
Deferrable job queue: retry and survive-an-outage behavior.

Uses a fake router (fails N times then succeeds) and an injected no-op sleep so
the backoff loop runs instantly.
"""

from __future__ import annotations

import asyncio

from app.jobs import JobQueue, JobStatus
from app.metadata import RequestMetadata
from app.providers import ProviderError
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ResponseMessage,
)


def a_meta() -> RequestMetadata:
    return RequestMetadata(
        tenant="acme",
        feature="batch",
        request_id="req-1",
        request_class="default",
        mode="deferrable",
    )


def a_request() -> ChatCompletionRequest:
    return ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="hi")])


def a_response() -> ChatCompletionResponse:
    return ChatCompletionResponse(
        model="m",
        choices=[Choice(index=0, message=ResponseMessage(content="ok"))],
        served_by="p",
    )


class FakeRouter:
    """Fails the first `fail_times` calls (simulating an outage), then succeeds."""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def complete(self, request, request_class=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError("all providers down")
        return a_response()


async def _drain(queue: JobQueue) -> None:
    await asyncio.wait_for(queue._queue.join(), timeout=5)


def test_deferred_job_survives_outage_and_succeeds():
    async def main():
        router = FakeRouter(fail_times=3)  # down for 3 attempts, then recovers
        sleeps: list[float] = []

        async def fake_sleep(d):
            sleeps.append(d)

        q = JobQueue(router, max_attempts=10, backoff_base_s=1, backoff_cap_s=30, sleep=fake_sleep)
        q.start()
        job = q.submit(a_request(), "default", a_meta())
        await _drain(q)
        await q.stop()
        return job, sleeps

    job, sleeps = asyncio.run(main())
    assert job.status is JobStatus.SUCCEEDED
    assert job.attempts == 4          # 3 failures + 1 success
    assert sleeps == [1, 2, 4]        # exponential backoff between failures


def test_deferred_job_fails_after_max_attempts():
    async def main():
        router = FakeRouter(fail_times=999)  # never recovers

        async def fake_sleep(d):
            pass

        q = JobQueue(router, max_attempts=3, backoff_base_s=1, sleep=fake_sleep)
        q.start()
        job = q.submit(a_request(), "default", a_meta())
        await _drain(q)
        await q.stop()
        return job

    job = asyncio.run(main())
    assert job.status is JobStatus.FAILED
    assert job.attempts == 3
    assert "down" in job.error


def test_on_success_callback_fires():
    async def main():
        router = FakeRouter(fail_times=1)
        seen = []

        async def fake_sleep(d):
            pass

        q = JobQueue(
            router,
            max_attempts=5,
            backoff_base_s=1,
            sleep=fake_sleep,
            on_success=lambda job: seen.append(job.id),
        )
        q.start()
        job = q.submit(a_request(), "default", a_meta())
        await _drain(q)
        await q.stop()
        return job, seen

    job, seen = asyncio.run(main())
    assert job.status is JobStatus.SUCCEEDED
    assert seen == [job.id]


if __name__ == "__main__":
    import sys

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
