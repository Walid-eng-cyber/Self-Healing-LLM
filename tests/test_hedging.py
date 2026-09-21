"""
Hedged requests for latency-sensitive classes.

Uses stub providers with controllable latency so we can assert the race outcome,
that the loser is cancelled, and that non-hedged classes never fire a second
provider.
"""

from __future__ import annotations

import asyncio

from app.providers import ProviderError, RouterProvider
from app.schemas import (
    ChatCompletionRequest,
    ChatCompletionResponse,
    ChatMessage,
    Choice,
    ResponseMessage,
)

REQ = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="hi")])


class StubProvider:
    """A provider that sleeps `delay` seconds, then succeeds or fails."""

    def __init__(self, name: str, delay: float, ok: bool = True) -> None:
        self.name = name
        self.delay = delay
        self.ok = ok
        self.started = False
        self.cancelled = False

    async def complete(self, request):
        self.started = True
        try:
            await asyncio.sleep(self.delay)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        if not self.ok:
            raise ProviderError(f"{self.name} failed")
        return ChatCompletionResponse(
            model="m",
            choices=[Choice(index=0, message=ResponseMessage(content=self.name))],
            served_by=self.name,
        )


def make_router(pool, hedge_ms=None):
    classes = {"fast": [p.name for p in pool]}
    return RouterProvider(pool, classes, "fast", hedge_ms=hedge_ms or {})


def run(coro):
    return asyncio.run(coro)


# --- primary answers within the window: no hedge ---------------------------

def test_fast_primary_does_not_hedge():
    primary = StubProvider("primary", delay=0.01)
    second = StubProvider("second", delay=0.01)
    router = make_router([primary, second], hedge_ms={"fast": 100})
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "primary"
    assert result.hedged is False
    assert second.started is False  # hedge never fired


# --- primary slow, hedge wins, loser cancelled -----------------------------

def test_hedge_fires_and_second_wins_cancelling_primary():
    primary = StubProvider("primary", delay=1.0)   # very slow
    second = StubProvider("second", delay=0.02)     # fast
    router = make_router([primary, second], hedge_ms={"fast": 50})
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "second"
    assert result.hedged is True
    assert second.started is True
    assert primary.cancelled is True  # loser was cancelled


# --- primary slow but still beats the hedge --------------------------------

def test_hedge_fires_but_primary_still_wins():
    primary = StubProvider("primary", delay=0.08)  # slow enough to hedge...
    second = StubProvider("second", delay=1.0)     # ...but hedge is slower
    router = make_router([primary, second], hedge_ms={"fast": 50})
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "primary"
    assert result.hedged is True       # a hedge WAS fired (primary was slow)
    assert second.started is True
    assert second.cancelled is True    # the hedge lost and was cancelled


# --- non-hedged class never fires a second provider ------------------------

def test_non_hedged_class_is_sequential():
    primary = StubProvider("primary", delay=0.05)
    second = StubProvider("second", delay=0.01)
    router = make_router([primary, second], hedge_ms={})  # nothing hedged
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "primary"
    assert result.hedged is False
    assert second.started is False


# --- both raced providers fail -> fall back to the rest --------------------

def test_both_hedged_fail_falls_back():
    primary = StubProvider("primary", delay=0.5, ok=False)
    second = StubProvider("second", delay=0.02, ok=False)
    third = StubProvider("third", delay=0.01, ok=True)
    router = make_router([primary, second, third], hedge_ms={"fast": 50})
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "third"
    assert result.hedged is True


# --- primary fails fast within the window -> failover, no hedge -------------

def test_primary_fails_fast_no_hedge():
    primary = StubProvider("primary", delay=0.01, ok=False)  # fails quickly
    second = StubProvider("second", delay=0.01, ok=True)
    router = make_router([primary, second], hedge_ms={"fast": 100})
    result = run(router.complete(REQ, request_class="fast"))
    assert result.served_by == "second"
    assert result.hedged is False  # primary resolved before the hedge timer


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
