# Circuit Breaker (Phase 2)

A circuit breaker sits in front of each provider and stops the gateway from
hammering an upstream that has gone bad. It is what turns "failover" into
"self-healing": the gateway notices a provider is failing, stops sending it
traffic on its own, and lets it back in once it recovers — no human involved.

Code: [`app/circuit_breaker.py`](../app/circuit_breaker.py). Each
`LiteLLMProvider` owns one breaker.

## The three states

```
  CLOSED ──(error rate > threshold  OR  p95 latency > budget)──► OPEN
    ▲                                                              │
    │                                                     (cooldown elapses)
    │                                                              ▼
    └──(enough trial successes)── HALF_OPEN ◄─────────────── (let a few trials in)
                                      │
                                (any trial fails)
                                      ▼
                                    OPEN
```

| State | Meaning | What a request does |
|-------|---------|---------------------|
| **CLOSED** | Normal. Outcomes are tracked in a sliding window. | Allowed through; outcome recorded. |
| **OPEN** | Tripped. The provider is considered down. | Skipped instantly ("fail fast") so the router fails over without waiting. |
| **HALF_OPEN** | Probation after the cooldown. | A few trial requests allowed; the rest skipped. |

## When it trips (CLOSED → OPEN)

After each recorded call, while CLOSED, the breaker checks — but only once it has
at least `min_requests` samples in the window. It trips if **either**:

1. **Error rate** over the window exceeds `error_rate_threshold`, or
2. **p95 latency** over the window exceeds `p95_budget_ms`.

The latency rule matters: a provider can be "up" (returning 200s) but so slow it
is effectively broken. p95 (the 95th-percentile latency) catches that without
overreacting to one slow request.

The reason it tripped is stored in `last_trip_reason` and shown in `/health`.

## When it recovers (OPEN → HALF_OPEN → CLOSED)

- While **OPEN**, every request fails fast until `open_cooldown_seconds` has
  passed.
- The next request moves the breaker to **HALF_OPEN** and is allowed through as a
  trial. Up to `half_open_max_calls` trials may run concurrently; extra requests
  still fail fast.
- If `half_open_successes_to_close` trials succeed, the breaker **closes** and
  clears its window (fresh start).
- If any trial **fails**, it goes straight back to **OPEN** and the cooldown
  restarts.

## How a request flows through a provider

In `LiteLLMProvider.complete()`:

```
1. healthy flag?      manual kill-switch (maintenance/demo). If off -> skip.
2. breaker.allow()?   if the circuit is open (and cooling down) -> skip (fail fast).
3. call + time it     run the LiteLLM request, measure latency.
4. breaker.record()   record success/failure + latency, which may trip or heal
                      the breaker for next time.
```

A "skip" raises `ProviderError`, which the `RouterProvider` catches and turns
into a failover to the next provider. So an open circuit simply means the router
moves on immediately instead of waiting for a timeout.

## Configuration

All via environment variables (defaults in `app/config.py`):

| Variable | Default | Meaning |
|----------|---------|---------|
| `CB_WINDOW_SECONDS` | 30 | Sliding window for error rate + latency |
| `CB_MIN_REQUESTS` | 5 | Minimum samples before the breaker may trip |
| `CB_ERROR_RATE_THRESHOLD` | 0.5 | Trip if error rate exceeds this (0–1) |
| `CB_P95_LATENCY_BUDGET_MS` | 2000 | Trip if p95 latency exceeds this |
| `CB_OPEN_COOLDOWN_SECONDS` | 15 | How long OPEN lasts before a trial |
| `CB_HALF_OPEN_MAX_CALLS` | 3 | Trial requests allowed while HALF_OPEN |
| `CB_HALF_OPEN_SUCCESSES_TO_CLOSE` | 2 | Trial successes needed to close |

> **Tuning note.** The 2000ms p95 budget suits cloud APIs. A local model
> (Ollama) can legitimately be slower than that on modest hardware — a real call
> here measured ~3.6s. If you keep a local provider, raise
> `CB_P95_LATENCY_BUDGET_MS`, or give slow-but-fine providers their own budget.

## Observability

`GET /health` reports each provider's circuit:

```json
{
  "name": "ollama",
  "mode": "live",
  "circuit": {
    "state": "closed",
    "samples": 1,
    "error_rate": 0.0,
    "p95_latency_ms": 3594.1,
    "last_trip_reason": null
  }
}
```

## Verifying it

Unit tests in [`tests/test_circuit_breaker.py`](../tests/test_circuit_breaker.py)
drive every transition with a fake clock (no sleeps): trips on error rate, trips
on p95 latency, does not trip below `min_requests` or at/under threshold, fails
fast while open, moves to half-open after cooldown, closes on trial successes,
reopens on a trial failure, limits trial calls, and prunes the window.

```bash
python -m pytest tests/ -q
```

End-to-end through the router, a broken provider trips and traffic fails over,
then recovers through half-open once repaired:

```
req 1: served_by=backup   primary.circuit=closed
req 2: served_by=backup   primary.circuit=closed
req 3: served_by=backup   primary.circuit=open      <- tripped
req 4: served_by=backup   primary.circuit=open      <- fail fast
...  (repair primary, cooldown elapses)
req 1: served_by=primary  primary.circuit=half_open <- trial
req 2: served_by=primary  primary.circuit=closed    <- recovered
```

## Why per-provider

Each provider gets its **own** breaker. One vendor's outage trips only that
vendor's circuit; the others keep serving. That isolation is the point — a bad
provider is quarantined, not allowed to affect the healthy ones.

## Failover order is per request class

When a breaker opens, the router does not just walk one global list — it walks
the preference list for the request's **class**. The class comes from the
optional `X-Request-Class` header (default: `default`).

Different kinds of request should fail over differently. Defined in
`REQUEST_CLASSES` in `app/config.py`:

| Class | Preference order | Rationale |
|-------|------------------|-----------|
| `classification` | ollama → gemini → openai | cheap/fast first; big model last |
| `generation` | openai → anthropic → ollama | quality first; local as last resort |
| `default` | ollama → openai → anthropic → gemini | the full pool in declared order |

So if `ollama`'s breaker is open, a **classification** request fails over to
`gemini`, while a **generation** request was never sending to `ollama` first
anyway and still starts at `openai`. Same outage, different reroute. An unknown
class falls back to the `default` list. The chosen class is also written to each
usage record, so cost can be attributed per class.
