"""
Per-provider circuit breaker.

A circuit breaker stops the gateway from repeatedly calling a provider that is
misbehaving. Each provider owns one breaker with three states:

    CLOSED     Normal. Requests flow. We record every outcome (success/failure
               and latency) in a sliding time window and, after each call, check
               whether the provider has become unhealthy.

    OPEN       Tripped. The provider is skipped immediately ("fail fast"), so the
               router fails over to the next provider without waiting. After a
               cooldown the breaker moves to HALF_OPEN to test the waters.

    HALF_OPEN  Probation. A few trial requests are allowed through. If enough
               succeed, the breaker closes (recovered). If one fails, it opens
               again (still broken).

The breaker trips (CLOSED -> OPEN) when, over the window and with at least
`min_requests` samples, EITHER:
    * the error rate exceeds `error_rate_threshold`, OR
    * the p95 latency exceeds `p95_budget_ms`.

The clock is injectable so the whole state machine can be tested without sleeps.
"""

from __future__ import annotations

import math
import random
import time
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Callable


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class _Outcome:
    at: float
    ok: bool
    latency_ms: float


class CircuitBreaker:
    def __init__(
        self,
        name: str,
        *,
        window_seconds: float = 30.0,
        min_requests: int = 5,
        error_rate_threshold: float = 0.5,
        p95_budget_ms: float = 2000.0,
        open_cooldown_seconds: float = 15.0,
        half_open_probe_ratio: float = 0.1,
        half_open_successes_to_close: int = 1,
        clock: Callable[[], float] = time.monotonic,
        rand: Callable[[], float] = random.random,
    ) -> None:
        self.name = name
        self.window_seconds = window_seconds
        self.min_requests = min_requests
        self.error_rate_threshold = error_rate_threshold
        self.p95_budget_ms = p95_budget_ms
        self.open_cooldown_seconds = open_cooldown_seconds
        # Fraction of traffic sent to a recovering provider as probes (0..1).
        self.half_open_probe_ratio = half_open_probe_ratio
        self.half_open_successes_to_close = half_open_successes_to_close
        self._clock = clock
        self._rand = rand

        self.state = CircuitState.CLOSED
        self._outcomes: deque[_Outcome] = deque()
        self._opened_at = 0.0
        self._half_open_successes = 0
        self.last_trip_reason: str | None = None

    # -- window bookkeeping -------------------------------------------------

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        while self._outcomes and self._outcomes[0].at < cutoff:
            self._outcomes.popleft()

    def _p95_latency(self) -> float:
        latencies = sorted(o.latency_ms for o in self._outcomes)
        if not latencies:
            return 0.0
        # Nearest-rank: the smallest value with >=95% of samples at or below it.
        rank = math.ceil(0.95 * len(latencies))
        return latencies[rank - 1]

    def _error_rate(self) -> float:
        if not self._outcomes:
            return 0.0
        failures = sum(1 for o in self._outcomes if not o.ok)
        return failures / len(self._outcomes)

    # -- decision: may a request go to this provider now? -------------------

    def allow(self) -> bool:
        """Called before a request. False means fail fast (skip this provider)."""
        now = self._clock()

        if self.state == CircuitState.OPEN:
            if now - self._opened_at >= self.open_cooldown_seconds:
                self._to_half_open()
            else:
                return False

        if self.state == CircuitState.HALF_OPEN:
            # Probe: send only a small fraction of traffic to the recovering
            # provider; route the rest away (fail fast to other providers).
            return self._rand() < self.half_open_probe_ratio

        return True  # CLOSED

    # -- recording the result of a call -------------------------------------

    def record(self, ok: bool, latency_ms: float) -> None:
        """Called after a request completes (success or failure)."""
        now = self._clock()
        self._outcomes.append(_Outcome(at=now, ok=ok, latency_ms=latency_ms))
        self._prune(now)

        if self.state == CircuitState.HALF_OPEN:
            if ok:
                # A probe succeeded -> the provider looks recovered. Close once
                # enough probes have passed (default: a single success closes).
                self._half_open_successes += 1
                if self._half_open_successes >= self.half_open_successes_to_close:
                    self._to_closed()
            else:
                # A probe failed -> reopen immediately, restart the cooldown.
                self._to_open(now, reason="half_open probe failed")
            return

        if self.state == CircuitState.CLOSED:
            reason = self._trip_reason()
            if reason:
                self._to_open(now, reason=reason)

    def _trip_reason(self) -> str | None:
        """Return why the breaker should trip, or None if it should stay closed."""
        n = len(self._outcomes)
        if n < self.min_requests:
            return None

        error_rate = self._error_rate()
        if error_rate > self.error_rate_threshold:
            return f"error_rate {error_rate:.0%} > {self.error_rate_threshold:.0%}"

        p95 = self._p95_latency()
        if p95 > self.p95_budget_ms:
            return f"p95 {p95:.0f}ms > {self.p95_budget_ms:.0f}ms"

        return None

    # -- transitions --------------------------------------------------------

    def _to_open(self, now: float, reason: str) -> None:
        self.state = CircuitState.OPEN
        self._opened_at = now
        self._half_open_successes = 0
        self.last_trip_reason = reason

    def _to_half_open(self) -> None:
        self.state = CircuitState.HALF_OPEN
        self._half_open_successes = 0

    def _to_closed(self) -> None:
        self.state = CircuitState.CLOSED
        self._outcomes.clear()  # fresh start after recovery
        self._half_open_successes = 0

    # -- observability ------------------------------------------------------

    def stats(self) -> dict:
        self._prune(self._clock())
        return {
            "state": self.state.value,
            "samples": len(self._outcomes),
            "error_rate": round(self._error_rate(), 3),
            "p95_latency_ms": round(self._p95_latency(), 1),
            "last_trip_reason": self.last_trip_reason,
        }
