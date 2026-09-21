# Phase 2 — Self-Healing Resilience

Phase 1 routed every model call through one gateway. Phase 2 makes that gateway
**resilient**: it detects failing providers, routes around them automatically,
brings them back on their own once they recover, and shaves the slow tail off
latency-sensitive traffic — all without a human touching anything.

This single document covers the whole of Phase 2. It is written to be understood
top to bottom.

**What Phase 2 delivers:**

1. **A circuit breaker per provider** — detects a bad provider (by error rate or
   p95 latency) and stops sending it traffic.
2. **Half-open probes** — automatically tests a recovering provider with a small
   trickle of traffic and heals it when it's back.
3. **Per-request-class failover** — different kinds of request fail over
   differently, each following its own provider preference list.
4. **Hedged requests** — for latency-sensitive classes, race a second provider
   after N ms and take the faster answer (with a documented cost trade-off).

Contents:
1. [The big picture](#1-the-big-picture)
2. [The circuit breaker](#2-the-circuit-breaker-per-provider)
3. [Half-open probes](#3-half-open-probes-automatic-recovery)
4. [Request classes & per-class failover](#4-request-classes--per-class-failover)
5. [Hedged requests & the cost trade-off](#5-hedged-requests--the-cost-trade-off)
6. [How a request flows end to end](#6-how-a-request-flows-end-to-end)
7. [Configuration reference](#7-configuration-reference)
8. [Testing & verification](#8-testing--verification)
9. [Summary](#9-summary)

---

## 1. The big picture

```
 client ─► metadata gate ─► ROUTER ─► provider pool (each with a circuit breaker) ─► cost record ─► response
                             │
                             ├─ picks the provider list for the request's CLASS
                             ├─ skips any provider whose BREAKER is open (failover)
                             └─ for latency-sensitive classes, HEDGES a slow primary
```

Everything in Phase 2 lives in the **router** and the **circuit breaker** that
guards each provider. The API surface from Phase 1 is unchanged — clients still
send an OpenAI-shaped request and get an OpenAI-shaped response.

Files:

| File | Phase 2 role |
|------|--------------|
| `app/circuit_breaker.py` | The breaker state machine (trip, probe, recover) |
| `app/providers.py` | `RouterProvider`: per-class failover + hedging; each `LiteLLMProvider` owns a breaker |
| `app/config.py` | Breaker thresholds, request-class preference lists, hedge settings |
| `app/accounting.py` | Usage records incl. request class, hedge flag, and cost |

---

## 2. The circuit breaker (per provider)

A circuit breaker stops the gateway from repeatedly calling a provider that has
gone bad. Each provider has its **own** breaker, so one vendor's outage is
quarantined and never drags down the healthy ones.

It is named after the electrical breaker in your house: when something goes
wrong, it **trips** and cuts the power instead of letting the wire overheat.

### The three states

```
  CLOSED ──(error rate > threshold  OR  p95 latency > budget)──► OPEN
    ▲                                                             │
    │                                                   (cooldown elapses)
    │                                                             ▼
    └────(a probe succeeds)──────────── HALF_OPEN ◄──── (send a small % as probes)
                                            │
                                     (a probe fails)
                                            ▼
                                          OPEN
```

| State | Meaning | What a request does |
|-------|---------|---------------------|
| **CLOSED** | Healthy/normal. Outcomes recorded in a sliding window. | Allowed through; result recorded. |
| **OPEN** | Tripped. Provider considered down. | Skipped instantly ("fail fast") → router fails over. |
| **HALF_OPEN** | Testing recovery after the cooldown. | A small % of traffic allowed as probes; the rest skip. |

> "Closed = working, open = broken" is counter-intuitive but standard, from
> electrical circuits: a *closed* circuit lets current flow; an *open* one stops
> it.

### When it trips (CLOSED → OPEN)

While CLOSED, the breaker keeps every recent outcome (success/failure and
latency) in a **sliding window** (default: last 30s). After each call — once it
has at least `min_requests` samples (default 5) — it trips if **either**:

1. **Error rate** over the window exceeds `error_rate_threshold` (default 50%), or
2. **p95 latency** over the window exceeds `p95_budget_ms` (default 2000ms).

The p95 rule matters: a provider can return 200s but be so slow it's effectively
broken. p95 ("95% of requests were at least this fast") catches a real slowdown
without overreacting to one slow request. The **minimum-samples** rule prevents a
single early failure ("1 of 1 = 100%!") from tripping on noise.

The reason it tripped is stored (`last_trip_reason`) and shown in `/health`.

---

## 3. Half-open probes (automatic recovery)

Tripping is easy; the self-healing part is deciding when a provider is
trustworthy again — safely, automatically.

When the cooldown passes, the breaker moves to **HALF_OPEN** and sends only a
**small percentage** of traffic to the recovering provider as **probes**
(`half_open_probe_ratio`, default 10%); the other ~90% still fail fast to other
providers. Trickling a little traffic — rather than flipping the provider fully
back on — protects a shaky provider from being flooded the instant it returns.

Each probe's result flips the breaker instantly:

```
   probe succeeds  ─────►  CLOSED   (recovered — use it fully again)
   probe fails     ─────►  OPEN     (still broken — wait out another cooldown)
```

- **Success → close** (default: a single success is enough), and the window is
  wiped for a fresh start.
- **Failure → reopen immediately**, and the cooldown restarts. A provider that is
  only "sort of" back does not get to limp along.

This automatic *trickle-and-decide* loop is what earns the gateway the name
"self-healing": it brings a recovered provider back on its own, risking only a
little traffic if it is still broken.

Measured: over 1000 requests to a still-broken provider in half-open, ~10% were
sent as probes and ~90% failed fast to a backup — matching the configured ratio.

---

## 4. Request classes & per-class failover

Before this, the gateway had one provider order for everybody. But a cheap
**classification** call ("spam: yes/no?") and an expensive **generation** call
("write three paragraphs") have opposite priorities and should **not** fail over
the same way.

So every request can carry a **class**, and each class has its own ordered list
of providers. The client sets the class with an optional header (keeping the
gateway OpenAI-compatible):

```
X-Request-Class: classification
```

The lists live in `REQUEST_CLASSES` ([`app/config.py`](../app/config.py)):

| Class | Preference order | Why |
|-------|------------------|-----|
| `classification` | ollama → gemini → openai | cheap/fast first; the big model is the last resort |
| `generation` | openai → anthropic → ollama | quality first; the local model only if the cloud is down |
| `default` | ollama → openai → anthropic → gemini | the full pool, for requests with no class |

The router walks *that class's* list — for both normal routing and failover —
skipping any provider whose breaker is open. An unknown class falls back to
`default`.

### The payoff: same outage, different reroute

If `ollama`'s breaker trips open:

- a **classification** request reroutes to `gemini` (next in its list),
- a **generation** request is unaffected — it started at `openai` anyway.

One outage, two different reroutes, each matching the class's priorities. That is
impossible with a single global order.

---

## 5. Hedged requests & the cost trade-off

Some classes care about **tail latency** — the occasional slow request that makes
a product feel laggy. Failover doesn't help (the provider hasn't *failed*, it's
just *slow*). **Hedging** does.

### How one hedged request works

```
t = 0 ms   Send to the primary provider.
           Answered before the timer? -> use it. Done. (No hedge.)

t = N ms   Primary still working -> ALSO send to the second provider.
           Race them: take whichever returns first, cancel the loser.
           Both fail -> fall back to the rest of the class's list.
```

`N` is the hedge delay. A smaller `N` hedges more requests (lower latency, more
spend); a larger `N` hedges fewer. The response carries a `hedged` flag.

### Opt-in per class

Hedging is enabled per class in `CLASS_HEDGE_MS` ([`app/config.py`](../app/config.py)):

```python
CLASS_HEDGE_MS = {
    "classification": 200,   # hedge after 200ms
    # "generation" is deliberately omitted -> never hedges
}
```

### ⚠️ The cost trade-off

**Hedging roughly doubles spend on the calls that actually hedge.** When the
timer fires, the request runs on **two providers at once**. You keep one answer,
but **both may bill** you — the loser is cancelled, but often only after it has
already generated (billable) tokens.

| Situation | Cost |
|-----------|------|
| Primary answers before the timer (no hedge) | same as before |
| Primary is slow, hedge fires | **~2×** |

That is why it is enabled **per class**:

| Class | Hedged? | Reasoning |
|-------|---------|-----------|
| `classification` | yes (200ms) | short, cheap calls where a slow tail hurts UX; 2× of cheap is still cheap |
| `generation` | no | long, expensive calls; paying twice is rarely worth it |

**Rule of thumb:** hedge cheap, fast, latency-sensitive classes; do not hedge
expensive, long-running ones. Each usage record includes `hedged` and
`estimated_cost_with_hedge_usd` (~2×) so the real bill stays visible.

---

## 6. How a request flows end to end

```
1. Metadata gate   Require X-Tenant-Id + X-Feature (400 if missing). Read the
                   optional X-Request-Class (default: "default"). Mint a
                   request id if absent.

2. Pick the list   The router resolves the request's class to its ordered
                   provider preference list.

3. Route           - Hedged class? Start the primary; if it is slow past N ms,
                     fire the second provider and race, cancelling the loser.
                   - Otherwise, try providers in order.
                   Either way, a provider whose circuit breaker is OPEN is
                   skipped instantly (fail fast) and the next one is tried.

4. Record          Each provider call feeds its breaker (success/failure +
                   latency), which may trip or heal it. The winning provider's
                   usage is logged with tenant, feature, class, hedge flag, and
                   estimated cost.

5. Respond         Return the OpenAI-shaped reply, with X-Request-Id echoed and
                   a non-standard served_by / hedged for observability.
```

---

## 7. Configuration reference

### Circuit breaker (per provider) — environment variables

| Variable | Default | Meaning |
|----------|---------|---------|
| `CB_WINDOW_SECONDS` | 30 | Sliding window for error rate + latency |
| `CB_MIN_REQUESTS` | 5 | Minimum samples before the breaker may trip |
| `CB_ERROR_RATE_THRESHOLD` | 0.5 | Trip if error rate exceeds this (0–1) |
| `CB_P95_LATENCY_BUDGET_MS` | 2000 | Trip if p95 latency exceeds this |
| `CB_OPEN_COOLDOWN_SECONDS` | 15 | How long OPEN lasts before probing |
| `CB_HALF_OPEN_PROBE_RATIO` | 0.1 | Fraction of traffic sent as probes while HALF_OPEN |
| `CB_HALF_OPEN_SUCCESSES_TO_CLOSE` | 1 | Probe successes needed to close |

> **Tuning note.** The 2000ms p95 budget suits cloud APIs. A local model (Ollama)
> can legitimately be slower (~3.6s here). Raise `CB_P95_LATENCY_BUDGET_MS` if you
> keep a local provider, or it will trip on honest local latency.

### Request classes & hedging — `app/config.py`

- `REQUEST_CLASSES` — `{class: [provider names in preference order]}`.
- `CLASS_HEDGE_MS` — `{class: hedge delay ms}`; classes absent here never hedge.

Both are visible at runtime: `GET /` returns `request_classes` and
`hedged_classes_ms`; `GET /health` returns each provider's circuit state.

---

## 8. Testing & verification

All state machines take an injectable clock (and the breaker an injectable random
source), so tests drive every transition deterministically — no sleeps.

```bash
python -m pytest tests/ -q      # 26 passed
```

- **`tests/test_circuit_breaker.py`** — trips on error rate; trips on p95 latency;
  does not trip below min samples or at/under threshold; fails fast while open;
  moves to half-open after cooldown; probe admitted when sampled / skipped when
  not; closes on a probe success; reopens on a probe failure; prunes the window.
- **`tests/test_class_routing.py`** — per-class preference order; failover follows
  the class list when a breaker is open; same outage reroutes differently by
  class; unknown class falls back; all-open-in-class errors.
- **`tests/test_hedging.py`** — fast primary (no hedge); hedge fires and the
  second wins (primary cancelled); hedge fires but the primary still wins (second
  cancelled); non-hedged class stays sequential; both raced fail → fallback;
  primary fails fast → failover without hedging.

Live snapshots:

```
# circuit breaker: trip then recover through half-open
req 3: served_by=backup   primary.circuit=open       (tripped on error rate)
...  (repaired, cooldown elapses)
req 1: served_by=primary  primary.circuit=half_open  (probe)
req 2: served_by=primary  primary.circuit=closed     (recovered)

# per-class routing (all healthy)
classification -> ollama    generation -> openai

# hedging (local primary too slow -> hedge wins)
classification -> served_by=gemini  hedged=True   242ms
generation     -> served_by=openai  hedged=False    5ms
```

---

## 9. Summary

Phase 2 turns the single-door gateway into a self-healing one:

- Each provider has a **circuit breaker** that trips on error rate or p95 latency
  and quarantines a bad provider.
- **Half-open probes** send a small % of traffic to a recovering provider and
  heal it automatically — close on a probe success, reopen on a failure.
- **Per-class failover** means a classification call and a generation call reroute
  differently, each down its own preference list.
- **Hedging** trims tail latency for latency-sensitive classes by racing a second
  provider, at a documented ~2× cost on the calls that hedge.

Together these make the gateway keep serving through provider outages and
slowdowns, recover on its own, and let you trade money for latency deliberately,
per class.
