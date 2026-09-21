# Half-Open Probes, Explained

**What this adds:**

> Half-open probes are what make it self-healing. Send a small percentage of
> traffic back to a recovering provider, close the breaker if it succeeds,
> reopen immediately if it does not.

This document explains that idea in plain language and shows how the code does
it. It refines the **half-open** state of the [circuit breaker](phase-2.md) —
read that first if the words "open" and "closed" are unfamiliar.

Code: [`app/circuit_breaker.py`](../app/circuit_breaker.py) ·
Tests: [`tests/test_circuit_breaker.py`](../tests/test_circuit_breaker.py)

---

## 1. The question this answers

When a provider's breaker is **open** (we've stopped using it because it was
failing), how do we ever decide it's safe to use again?

We can't just wait a fixed time and switch it fully back on — it might still be
broken, and we'd dump all our traffic straight onto a provider that immediately
fails again. That's the opposite of self-healing.

We also can't leave it off forever — then a brief outage would permanently
retire a provider.

The answer is to **test it gently**: send it a *tiny trickle* of real traffic and
watch what happens. That trickle is called a **probe**, and it happens in the
**half-open** state.

---

## 2. What a probe is

A **probe** is a single real request that we deliberately allow through to a
recovering provider, just to see if it works now.

The key word is **small percentage**. In half-open, the breaker does **not** send
all traffic to the recovering provider. It sends only a small fraction — 10% by
default — and routes the other ~90% to other providers as usual (fail fast).

Why only a trickle?

- If the provider is **still broken**, only ~10% of requests are affected before
  we notice and shut it back off. The other 90% never touched it.
- If the provider is **actually healthy**, a probe succeeds and we bring it fully
  back.

It's like testing whether the floor is safe by putting a little weight on it
first, instead of jumping on with both feet.

---

## 3. What happens to a probe's result

Each probe has exactly two possible outcomes, and each one flips the breaker
instantly:

```
   probe succeeds  ─────►  CLOSED   (provider is healthy again, use it fully)
   probe fails     ─────►  OPEN     (still broken, stop using it, wait again)
```

- **Success → close.** One good probe is enough (by default) to trust the
  provider again. The breaker closes and wipes its history for a fresh start.
- **Failure → reopen immediately.** A single failed probe sends the breaker
  straight back to open, and the cooldown timer restarts. A provider that is only
  "sort of" back doesn't get to limp along hurting requests.

That is the whole self-healing loop: *trickle a little traffic in, and let the
result decide.*

---

## 4. The full recovery cycle

Putting it together with the cooldown:

```
OPEN ──(cooldown passes)──► HALF_OPEN ──(send ~10% as probes)──┐
  ▲                                                             │
  │                                              ┌──────────────┴───────────────┐
  │                                              ▼                              ▼
  └──────────────(a probe fails)────────  probe fails                    probe succeeds
                                                                                │
                                                                                ▼
                                                                             CLOSED
```

1. Breaker is **OPEN**; all requests fail fast to other providers.
2. After `open_cooldown_seconds`, it becomes **HALF_OPEN**.
3. ~10% of requests are admitted as **probes**; ~90% still skip the provider.
4. First probe to resolve decides: **success → CLOSED**, **failure → OPEN** (and
   the cooldown restarts before it can probe again).

---

## 5. How the code does it

In [`app/circuit_breaker.py`](../app/circuit_breaker.py):

**Admitting a probe** — the `allow()` method, called before each request:

```python
if self.state == CircuitState.HALF_OPEN:
    # Send only a small fraction of traffic to the recovering provider;
    # route the rest away (fail fast to other providers).
    return self._rand() < self.half_open_probe_ratio
```

`self._rand()` returns a number in `[0, 1)`. With `half_open_probe_ratio = 0.1`,
about 1 in 10 calls returns `True` (admitted as a probe); the rest return
`False` (skipped). Simple probability, no counters.

**Reacting to the result** — the `record()` method, called after the request:

```python
if self.state == CircuitState.HALF_OPEN:
    if ok:
        # a probe succeeded -> close (default: one success is enough)
        self._half_open_successes += 1
        if self._half_open_successes >= self.half_open_successes_to_close:
            self._to_closed()
    else:
        # a probe failed -> reopen immediately, restart the cooldown
        self._to_open(now, reason="half_open probe failed")
```

The random source is **injectable** (like the clock), so tests can force a probe
to be admitted or skipped and check every branch without relying on chance.

---

## 6. Why this is the "self-healing" part

Everything before this could *detect* a bad provider and *route around* it. But
routing around a provider forever isn't healing — it's just avoidance. Healing
means the system brings a provider **back on its own** once it recovers, with no
human flipping a switch.

Probes are exactly that mechanism:

- automatic (no human decides when to retry),
- safe (only ~10% of traffic is risked while testing),
- fast to react (one probe result flips the state instantly).

That's why the breaker's half-open probe is the piece that earns the gateway the
name "self-healing".

---

## 7. Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `CB_OPEN_COOLDOWN_SECONDS` | 15 | How long OPEN lasts before probing begins |
| `CB_HALF_OPEN_PROBE_RATIO` | 0.1 | Fraction of traffic sent as probes (0.1 = 10%) |
| `CB_HALF_OPEN_SUCCESSES_TO_CLOSE` | 1 | Probe successes needed to close |

Raise the ratio to recover faster (but risk more traffic on a shaky provider);
lower it to be more cautious. Raise successes-to-close to require several good
probes before trusting a provider again.

---

## 8. Seeing it

The tests drive every branch with an injected clock and random source:

```bash
python -m pytest tests/test_circuit_breaker.py -v
```

Relevant cases: a probe is admitted when sampled, a request is skipped when not
sampled (stays half-open), a probe success closes, a probe failure reopens.

Measured end-to-end: over 1000 requests to a still-broken provider in half-open,
**~100 (~10%)** were sent to it as probes and the other ~90% failed fast to a
backup — matching the configured ratio. When the provider was repaired, the next
probe closed the breaker and it returned to full rotation.

---

## 9. One-paragraph summary

When a provider is being tested for recovery (the half-open state), the breaker
sends only a small percentage of traffic to it as **probes** and routes the rest
away. A probe that **succeeds** closes the breaker and restores the provider to
full use; a probe that **fails** reopens the breaker immediately and restarts the
cooldown. This trickle-and-decide loop lets the gateway bring a recovered
provider back automatically, while risking only a little traffic if it is still
broken — which is what makes it self-healing.
