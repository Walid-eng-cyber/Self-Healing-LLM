# Phase 2 — The Circuit Breaker, Explained

**Goal of this phase:**

> Implement a circuit breaker per provider with closed, open, and half-open
> states. Trip on error rate above threshold within the window, or p95 latency
> above budget.

This document explains what that means, why it matters, and exactly how the code
does it — in plain language. Read it top to bottom and you will understand the
whole thing.

Code: [`app/circuit_breaker.py`](../app/circuit_breaker.py) ·
Tests: [`tests/test_circuit_breaker.py`](../tests/test_circuit_breaker.py)

---

## 1. What problem does this solve?

In Phase 1 the gateway learned to **fail over**: if a provider errors on a
request, try the next one. That helps, but it has two weaknesses:

1. **It only reacts to the request in front of it.** Every single request still
   gets sent to the broken provider first, waits for it to fail, and only *then*
   moves on. If the provider is down, you pay that "wait then fail" cost on
   *every* call.
2. **It has no memory.** It cannot notice "this provider has failed 8 of the
   last 10 times — stop sending it traffic for a while."

A **circuit breaker** fixes both. It watches how a provider is behaving over
time, and when the provider is clearly unhealthy it **stops sending it traffic
entirely** for a cooldown period — so requests skip it *instantly* instead of
waiting to fail. Then it carefully checks whether the provider has recovered
before trusting it again.

### The real-world metaphor

It is named after the electrical circuit breaker in your house. When something
goes wrong (a short circuit), the breaker **trips** and cuts the power, instead
of letting the wire overheat and start a fire. Once the problem is fixed, you
**reset** it. Same idea here: when a provider misbehaves, we "cut the power" to
it for a while.

---

## 2. The three states

The breaker for each provider is always in one of three states:

```
  CLOSED ──(too many errors  OR  too slow)──► OPEN
    ▲                                           │
    │                                  (wait out the cooldown)
    │                                           ▼
    └────(a probe succeeds)──────────── HALF_OPEN
                                            │
                                    (a probe fails)
                                            ▼
                                          OPEN
```

- **CLOSED** = healthy / normal.
  Requests flow through. The breaker quietly records the result of each one
  (did it succeed? how long did it take?).

- **OPEN** = tripped / "do not use".
  The provider is considered broken. Requests **skip it immediately** (this is
  called *failing fast*) so the router moves straight to the next provider. The
  breaker stays open for a cooldown period.

- **HALF_OPEN** = testing the waters.
  After the cooldown, the breaker cautiously lets a *few* requests through as a
  test. If they succeed, the provider is back — close the breaker. If one fails,
  it is still broken — open again.

> **Why the names?** "Closed" and "open" come from electrical circuits: a
> *closed* circuit lets current flow (requests pass); an *open* circuit is
> broken (requests stop). "Half-open" is the in-between trial state. It is
> counter-intuitive at first — closed = working, open = broken — but it is the
> standard terminology.

---

## 3. When does it trip? (CLOSED → OPEN)

While the breaker is CLOSED, it keeps a list of recent outcomes in a **sliding
window** — by default, everything from the last 30 seconds. Older results fall
out of the window and stop counting.

After each call, it asks: *"based on this window, is the provider unhealthy?"*
It trips to OPEN if **either** of these is true:

### Trip condition 1 — error rate too high

> "Trip on error rate above threshold within the window."

It counts how many calls in the window failed. If the failure rate is above the
threshold (default **50%**), it trips.

Example: in the last 30 seconds there were 10 calls and 6 failed → 60% error
rate → above 50% → **trip.**

### Trip condition 2 — too slow (p95 latency)

> "or p95 latency above budget."

Even if calls *succeed*, a provider that takes forever is effectively broken. So
the breaker also watches latency — specifically the **p95 latency**.

**What is p95?** If you line up the last N response times from fastest to
slowest, the p95 is the value 95% of the way up. In plain terms: "95% of requests
were at least this fast; only the slowest 5% were worse." It is a good health
signal because it ignores one-off blips (a single slow request won't trip it) but
catches a genuine slowdown (most requests getting slow).

If the p95 latency in the window is above the budget (default **2000ms**), it
trips.

### The safety rule: minimum samples

The breaker will **not** trip until it has seen at least `min_requests` calls
(default **5**) in the window. Without this, a single failure ("1 out of 1 =
100% error rate!") would trip the breaker instantly. Requiring a handful of
samples first means it reacts to a *pattern*, not to noise.

When it trips, it remembers **why** (e.g. `"error_rate 60% > 50%"`) — visible in
`/health` as `last_trip_reason`.

---

## 4. How does it recover? (OPEN → HALF_OPEN → CLOSED)

Tripping is easy; the clever part is deciding when the provider is trustworthy
again. The breaker does this gradually, so it never dumps full traffic onto a
provider that is still shaky.

1. **OPEN — fail fast.**
   For `open_cooldown_seconds` (default **15s**) after tripping, every request
   skips this provider instantly. No calls are sent; the router uses other
   providers.

2. **HALF_OPEN — a careful test with probes.**
   Once the cooldown passes, the breaker sends only a **small percentage** of
   traffic back to the recovering provider as **probes**
   (`half_open_probe_ratio`, default **10%**); the other ~90% still skip it and
   go to other providers. Trickling a little traffic — rather than flipping the
   provider fully back on — protects a fragile provider from being flooded the
   instant it comes back.

3. **Recover or relapse.**
   - A probe **succeeds** → the provider looks healthy → **CLOSED** (default: a
     single success is enough), and the window is wiped clean for a fresh start.
   - A probe **fails** → straight back to **OPEN** immediately, and the cooldown
     restarts. (A provider that is "sort of" back does not get to limp along.)

---

## 5. How a request actually flows through it

Every call to a provider goes through these four steps
(`LiteLLMProvider.complete()` in [`app/providers.py`](../app/providers.py)):

```
1. healthy flag?     A manual on/off switch for maintenance or demos.
                     If off -> skip this provider.

2. breaker.allow()?  Ask the breaker if a request may go through right now.
                     - CLOSED    -> yes
                     - OPEN      -> no (unless the cooldown just elapsed)
                     - HALF_OPEN -> yes for a small % of traffic (probes)
                     If "no" -> skip this provider (fail fast).

3. call + time it    Send the request to the provider via LiteLLM, and measure
                     how long it took.

4. breaker.record()  Tell the breaker the outcome (success/failure + latency).
                     This is what may trip the breaker, or heal it.
```

When step 1 or 2 says "skip", the provider raises a `ProviderError`. The
`RouterProvider` catches that and moves to the next provider in the pool. So an
open circuit simply means **the router fails over instantly, without waiting.**

That is the whole payoff: a broken provider costs you nothing per request,
because you stop knocking on its door until it is likely to answer.

---

## 5b. Failover follows the request "class"

When a breaker opens, which provider gets the traffic next? That depends on the
**request class** — because not all requests should fail over the same way.

A cheap, high-volume **classification** call and an expensive **long-form
generation** call have different priorities. So each class has its own ordered
preference list of providers (`REQUEST_CLASSES` in
[`app/config.py`](../app/config.py)):

| Class | Preference order | Why |
|-------|------------------|-----|
| `classification` | ollama → gemini → openai | cheap and fast first; the expensive model is the last resort |
| `generation` | openai → anthropic → ollama | quality first; the small local model only if the cloud is down |
| `default` | the full pool in order | used when no class is given |

The client picks the class with an optional `X-Request-Class` header. The router
then walks *that* list, skipping any provider whose breaker is open.

Concretely: if `ollama`'s breaker trips **open** —

- a `classification` request reroutes to **gemini** (next in its list),
- a `generation` request is unaffected — it started at `openai` anyway.

Same outage, two different reroutes. That is the whole point: failover order is a
property of the request class, not one fixed order for everyone.

## 6. Per-provider — and why that matters

Each provider gets its **own** breaker. OpenAI's breaker knows nothing about
Anthropic's. So if OpenAI has an outage:

- OpenAI's breaker trips → OpenAI is skipped.
- Anthropic, Gemini, and the local model keep serving normally.

One bad provider is **quarantined**; it cannot drag down the healthy ones. That
isolation is exactly why the breaker is per-provider and not one global switch.

---

## 7. The settings you can tune

All configurable via environment variables (defaults in
[`app/config.py`](../app/config.py)):

| Variable | Default | Plain meaning |
|----------|---------|---------------|
| `CB_WINDOW_SECONDS` | 30 | How far back "recent" goes |
| `CB_MIN_REQUESTS` | 5 | Need at least this many calls before judging |
| `CB_ERROR_RATE_THRESHOLD` | 0.5 | Trip above this failure rate (0.5 = 50%) |
| `CB_P95_LATENCY_BUDGET_MS` | 2000 | Trip if p95 latency exceeds this |
| `CB_OPEN_COOLDOWN_SECONDS` | 15 | How long to stay tripped before testing |
| `CB_HALF_OPEN_PROBE_RATIO` | 0.1 | Fraction of traffic sent as probes while testing |
| `CB_HALF_OPEN_SUCCESSES_TO_CLOSE` | 1 | Probe wins needed to fully recover |

> **A real tuning gotcha.** The 2000ms latency budget suits fast cloud APIs. The
> local Ollama model on this machine measured ~3.6s for one call — legitimately
> slower. With a local provider you would raise `CB_P95_LATENCY_BUDGET_MS`, or
> give slow-but-fine providers their own budget, so the breaker does not trip a
> provider that is simply doing heavier work locally.

---

## 8. Seeing it in action

### In `/health`

Every provider reports its circuit live:

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

### The tests (every transition, no waiting)

The breaker takes its clock as an input, so the tests can *fast-forward time*
instead of sleeping. That lets them prove the full lifecycle in milliseconds:

```bash
python -m pytest tests/test_circuit_breaker.py -v
```

11 tests, one per behavior: starts closed, does not trip below the minimum
sample count, trips on error rate, does not trip at/under the threshold, trips on
p95 latency, fails fast while open, moves to half-open after the cooldown, closes
on a probe success, reopens on a probe failure, admits only the sampled
fraction of traffic as probes, and prunes old results out of the window.

### End-to-end through the router

A broken provider trips and traffic fails over; once repaired, it recovers
through half-open:

```
req 1: served_by=backup   primary.circuit=closed
req 2: served_by=backup   primary.circuit=closed
req 3: served_by=backup   primary.circuit=open       <- tripped (error rate)
req 4: served_by=backup   primary.circuit=open       <- failing fast
...  (primary repaired, cooldown elapses)
req 1: served_by=primary  primary.circuit=half_open  <- trial request
req 2: served_by=primary  primary.circuit=closed     <- recovered
```

---

## 9. One-paragraph summary

Each provider has a circuit breaker that watches its recent calls in a sliding
window. If too many fail (error rate over threshold) or they get too slow (p95
latency over budget), the breaker **trips open** and the router skips that
provider instantly instead of waiting for it to fail. After a cooldown the
breaker lets a few **trial** requests through; if they succeed it **closes** and
the provider is trusted again, and if they fail it stays open. Because each
provider has its own breaker, one provider's outage never affects the others.
That automatic trip-and-heal, per provider, is what makes the gateway
self-healing.
