# Request Classes & Per-Class Failover, Explained

**What this adds:**

> When a breaker opens, route to the next provider in the preference list for
> that request class. Preference lists differ by class, since a cheap
> classification call and a long-form generation call should not fail over the
> same way.

This document explains that idea in plain language and shows exactly how the code
does it. It builds on the [circuit breaker](phase-2.md) — read that first if the
words "breaker opens" are unfamiliar.

Code: [`app/config.py`](../app/config.py) (the lists) ·
[`app/providers.py`](../app/providers.py) (the routing) ·
Tests: [`tests/test_class_routing.py`](../tests/test_class_routing.py)

---

## 1. The problem: one failover order does not fit everyone

Before this change, the gateway had **one** provider order for everybody. If the
first provider's breaker tripped, every request fell back to the same second
provider, then the same third, and so on.

But not all requests are alike. Think about two very different calls:

- **A classification call** — "is this email spam: yes or no?" Tiny input, tiny
  output, run millions of times. You want it **cheap and fast**. A big expensive
  model is overkill.
- **A long-form generation call** — "write a three-paragraph product
  description." You want **quality**. A tiny local model would give a worse
  answer.

If the provider these two prefer goes down, they should **not** fall back to the
same place. The classification call should drop to another cheap option; the
generation call should drop to another strong model. One global order can't do
that — it would force one of them to fail over badly.

The fix: give each **class** of request its own ordered list of providers.

---

## 2. What is a "request class"?

A request class is just a label the caller puts on a request to say *what kind*
of call it is. The client sets it with an HTTP header:

```
X-Request-Class: classification
```

It is **optional**. If the caller doesn't set it, the request uses the `default`
class. Using a header (rather than a body field) keeps the gateway
OpenAI-compatible — the client sets it once via `default_headers`, exactly like
the tenant and feature headers.

---

## 3. The preference lists

Each class maps to an ordered list of provider **names**. This lives in
`REQUEST_CLASSES` in [`app/config.py`](../app/config.py):

| Class | Order it tries providers | Why this order |
|-------|--------------------------|----------------|
| `classification` | ollama → gemini → openai | Cheapest/fastest first (local, then cheap cloud). The expensive model is the last resort. |
| `generation` | openai → anthropic → ollama | Strongest model first. The small local model only if the cloud is down. |
| `default` | ollama → openai → anthropic → gemini | The full pool in declared order, for requests with no class. |

"Order it tries providers" is the whole point: it is both the **normal**
preference and the **failover** path. The router starts at the top and moves
down.

---

## 4. How a request flows now

When a request arrives, the router:

1. Reads the request's class (from `X-Request-Class`, or `default`).
2. Looks up that class's provider list.
3. Walks the list top to bottom:
   - The provider's circuit breaker is **open**? Skip it instantly, go to the
     next one in *this class's* list.
   - The provider errors? Same — skip to the next.
   - The provider succeeds? Return its answer.
4. If every provider in the class's list is unavailable, return an error that
   names the class.

In code ([`app/providers.py`](../app/providers.py)):

```python
async def complete(self, request, request_class=None):
    request_class = request_class or self.default_class
    providers = self.providers_for(request_class)   # the class's ordered list
    for provider in providers:
        try:
            return await provider.complete(request)  # breaker-open -> ProviderError
        except ProviderError:
            continue                                 # next in THIS class's list
    raise ProviderError(f"all providers failed for class '{request_class}' ...")
```

The connection to the circuit breaker is the key bit: an **open breaker** makes
`provider.complete()` raise `ProviderError`, and the router treats that exactly
like a failure — it moves to the next provider in the class's list. So "when a
breaker opens, route to the next provider for that class" falls out naturally.

---

## 5. The payoff: same outage, different reroute

This is the behavior the whole feature exists for. Suppose **ollama's breaker
trips open** (say the local server got slow and crossed the latency budget):

- A **classification** request — its list is `ollama → gemini → openai` — skips
  the open ollama and reroutes to **gemini** (still cheap).
- A **generation** request — its list is `openai → anthropic → ollama` — never
  wanted ollama first anyway, so it is **unaffected** and still starts at
  **openai**.

One provider is down, and the two classes react completely differently — each
according to its own priorities. That is impossible with a single global order.

---

## 5c. Hedged requests for latency-sensitive classes

Some classes care about **tail latency** — the occasional slow request that makes
a product feel sluggish. For those, the gateway can **hedge**: if the primary
provider hasn't answered within N milliseconds, it fires a second provider too,
takes whichever returns first, and cancels the loser.

Which classes hedge (and after how many ms) is set in `CLASS_HEDGE_MS`
([`app/config.py`](../app/config.py)):

```python
CLASS_HEDGE_MS = {
    "classification": 200,   # latency-sensitive -> hedge after 200ms
    # "generation" is deliberately omitted (see the cost note below)
}
```

Classes not listed here are never hedged; they use plain sequential failover.

### How one hedged request flows

```
t=0ms     start primary (first provider in the class list)
          primary answers before N ms?  -> return it, no hedge, done
t=N ms    primary still running -> ALSO start the second provider
          race them: first success wins, the loser is cancelled
          both fail -> fall back to the rest of the class's list
```

The response carries `hedged: true/false` so you can see whether a hedge fired.

### ⚠️ The cost trade-off (important)

**Hedging roughly doubles spend on the calls that actually hedge.** When the
timer fires, the request runs on *two* providers at once. You only use one
answer, but both may bill you — the loser can be cancelled after it has already
done work (generated tokens), and providers commonly charge for that.

So hedging trades **money for latency**:

- A call that answers before the timer costs the same as before (no hedge fired).
- A call that hedges costs about **2×**.

That is why it is enabled **per class**, not globally:

| Class | Hedged? | Why |
|-------|---------|-----|
| `classification` | yes (200ms) | short, cheap calls where a slow tail hurts UX; 2× of "cheap" is still cheap |
| `generation` | no | long, expensive calls; paying twice for a big generation is rarely worth it |

Rule of thumb: hedge **cheap, fast, latency-sensitive** classes; do **not** hedge
**expensive, long-running** ones. Tune the delay per class — a larger N hedges
fewer calls (less extra spend, less latency benefit); a smaller N hedges more.

The usage record makes the cost visible: it includes `hedged`, the winner's
`estimated_cost_usd`, and `estimated_cost_with_hedge_usd` (~2× when hedged) so
you can track the real bill.

## 6. A bonus: cost by class

Because the class flows through the same metadata path as tenant and feature, it
is also written into every usage record:

```json
{"tenant": "acme", "feature": "inbox", "request_class": "classification",
 "served_by": "ollama", "total_tokens": 32, "estimated_cost_usd": 0.0}
```

So you can now answer not just "which tenant spent what" but "how much are we
spending on classification vs generation" — useful, since those have very
different cost profiles.

---

## 7. Seeing it

The preference lists are visible at the root endpoint:

```bash
curl http://localhost:8000/
# {... "request_classes": {"classification": ["ollama","gemini","openai"], ...}}
```

Send a request with a class header:

```bash
curl http://localhost:8000/v1/chat/completions \
  -H "X-Tenant-Id: acme" -H "X-Feature: inbox" \
  -H "X-Request-Class: classification" \
  -H "Content-Type: application/json" \
  -d '{"model":"m","messages":[{"role":"user","content":"hi"}]}'
# served_by: ollama   (first in the classification list)
```

The tests prove the routing, including the same-outage-different-reroute case,
with breakers forced open — no waiting:

```bash
python -m pytest tests/test_class_routing.py -v
```

---

## 8. One-paragraph summary

Every request can carry a **class** (via `X-Request-Class`), and each class has
its own ordered list of providers. The router follows that list — for both normal
routing and failover — skipping any provider whose circuit breaker is open. So
when a provider goes down, a cheap classification call and an expensive
generation call reroute to *different* backups, each matching its own priorities,
instead of being forced down one shared path.
