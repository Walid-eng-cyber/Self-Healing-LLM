"""
Circuit breaker state-machine tests.

A fake clock lets us drive every transition (including the open->half_open
cooldown) instantly, with no real sleeps.
"""

from __future__ import annotations

from app.circuit_breaker import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def make_breaker(clock: FakeClock, rand=lambda: 0.0, **overrides) -> CircuitBreaker:
    # Default rand=0.0 always samples below the probe ratio -> a probe is always
    # admitted, which keeps the recovery tests deterministic. Probe-sampling
    # tests pass their own rand.
    kwargs = dict(
        window_seconds=30.0,
        min_requests=5,
        error_rate_threshold=0.5,
        p95_budget_ms=2000.0,
        open_cooldown_seconds=15.0,
        half_open_probe_ratio=0.1,
        half_open_successes_to_close=1,
        clock=clock,
        rand=rand,
    )
    kwargs.update(overrides)
    return CircuitBreaker("test", **kwargs)


def feed(breaker: CircuitBreaker, outcomes: list[tuple[bool, float]]) -> None:
    for ok, latency in outcomes:
        # Emulate the provider flow: only record if allowed through.
        if breaker.allow():
            breaker.record(ok=ok, latency_ms=latency)


# --- CLOSED behavior --------------------------------------------------------

def test_starts_closed_and_allows():
    breaker = make_breaker(FakeClock())
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True


def test_below_min_requests_never_trips():
    breaker = make_breaker(FakeClock())
    # 4 failures, but min_requests is 5 -> not enough data to judge.
    feed(breaker, [(False, 10)] * 4)
    assert breaker.state is CircuitState.CLOSED


def test_trips_on_error_rate():
    breaker = make_breaker(FakeClock())
    # 5 calls, 3 failures = 60% > 50% threshold -> OPEN.
    feed(breaker, [(False, 10), (False, 10), (False, 10), (True, 10), (True, 10)])
    assert breaker.state is CircuitState.OPEN
    assert "error_rate" in breaker.last_trip_reason


def test_does_not_trip_at_or_below_threshold():
    breaker = make_breaker(FakeClock())
    # 5 calls, 2 failures = 40% (< 50%) and fast -> stays CLOSED.
    feed(breaker, [(False, 10), (False, 10), (True, 10), (True, 10), (True, 10)])
    assert breaker.state is CircuitState.CLOSED


def test_trips_on_p95_latency():
    breaker = make_breaker(FakeClock())
    # All succeed, but latencies are huge -> p95 over budget -> OPEN.
    feed(breaker, [(True, 5000)] * 5)
    assert breaker.state is CircuitState.OPEN
    assert "p95" in breaker.last_trip_reason


# --- OPEN behavior ----------------------------------------------------------

def test_open_fails_fast_during_cooldown():
    clock = FakeClock()
    breaker = make_breaker(clock)
    feed(breaker, [(False, 10)] * 5)  # trip it
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False  # still within cooldown -> skip provider


def test_open_moves_to_half_open_after_cooldown():
    clock = FakeClock()
    breaker = make_breaker(clock)
    feed(breaker, [(False, 10)] * 5)
    clock.advance(16)  # past the 15s cooldown
    assert breaker.allow() is True
    assert breaker.state is CircuitState.HALF_OPEN


# --- HALF_OPEN behavior -----------------------------------------------------

def test_half_open_probe_success_closes():
    clock = FakeClock()
    breaker = make_breaker(clock)  # rand=0 -> probe admitted; 1 success closes
    feed(breaker, [(False, 10)] * 5)
    clock.advance(16)
    assert breaker.allow() is True          # probe admitted
    breaker.record(ok=True, latency_ms=10)  # probe succeeds
    assert breaker.state is CircuitState.CLOSED


def test_half_open_probe_failure_reopens():
    clock = FakeClock()
    breaker = make_breaker(clock)
    feed(breaker, [(False, 10)] * 5)
    clock.advance(16)
    assert breaker.allow() is True           # probe admitted
    breaker.record(ok=False, latency_ms=10)  # probe fails -> immediately OPEN
    assert breaker.state is CircuitState.OPEN


def test_half_open_admits_only_sampled_fraction():
    clock = FakeClock()
    # rand returns 0.5, above the 0.1 probe ratio -> this request is NOT a probe.
    breaker = make_breaker(clock, rand=lambda: 0.5)
    feed(breaker, [(False, 10)] * 5)
    clock.advance(16)
    assert breaker.allow() is False               # routed away, not a probe
    assert breaker.state is CircuitState.HALF_OPEN  # still testing the waters


def test_half_open_admits_probe_when_sampled():
    clock = FakeClock()
    # rand returns 0.05, below the 0.1 probe ratio -> admitted as a probe.
    breaker = make_breaker(clock, rand=lambda: 0.05)
    feed(breaker, [(False, 10)] * 5)
    clock.advance(16)
    assert breaker.allow() is True


# --- window pruning ---------------------------------------------------------

def test_old_outcomes_leave_the_window():
    clock = FakeClock()
    breaker = make_breaker(clock)
    feed(breaker, [(False, 10)] * 4)   # 4 failures
    clock.advance(31)                  # window is 30s -> those age out
    breaker.record(ok=True, latency_ms=10)
    stats = breaker.stats()
    assert stats["samples"] == 1
    assert breaker.state is CircuitState.CLOSED


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
