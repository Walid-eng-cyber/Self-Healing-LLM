# Phase 3 — Queue & Retry Deferrable Work

Phase 2 made the gateway resilient to a provider *misbehaving* (circuit breakers,
failover, probes, hedging). But when **every** provider is down, an interactive
call has nowhere to go — it must fail. Phase 3 adds a second path for work that
**doesn't need an answer right now**: queue it and retry it until a provider
recovers, so it survives the outage instead of failing.

This document covers Phase 3 and grows as more of it lands.

**Status:** in progress.

- ✅ Classify requests as interactive or deferrable at the API boundary;
  interactive fails fast, deferrable survives an outage.
- ⬜ (next) durable, restart-safe queue (e.g. Redis); idempotency so retries
  don't double-charge.

---

## 1. The core idea: two kinds of request

Not all requests have the same urgency:

- **Interactive** — a user is waiting for the reply (a chat box, a search).
  If it can't be served, the caller needs to know *now*. Waiting silently would
  be worse than a clear, fast error. → **fail fast.**
- **Deferrable** — no human is blocked on it (a nightly batch summary, an
  offline enrichment job, a webhook you'll process eventually). If providers are
  down, the right thing is to **keep the work and try again later**, not throw it
  away. → **survive the outage.**

The gateway lets the caller say which one each request is, and then treats it
accordingly.

---

## 2. Classifying at the API boundary

The caller sets one header:

```
X-Request-Mode: interactive     # default
X-Request-Mode: deferrable
```

It's read in the same metadata dependency as tenant/feature/request-id
([`app/metadata.py`](../app/metadata.py)), so it stays OpenAI-compatible — set it
once via `default_headers` and nothing else changes. If the header is absent or
unrecognized, the mode defaults to **interactive** — the safe choice, because
interactive never changes the response contract (you always get a completion or
an error, never a "come back later").

"At the API boundary" matters: the decision is made **once, where the request
enters**, and everything downstream just follows it. The caller — who knows
whether a human is waiting — owns the classification.

---

## 3. What each mode does

Both modes try to serve the request normally first (through the Phase 2 failover
pool). They only differ in **what happens when every provider is unavailable**:

```
             ┌─ try the request through the failover pool ─┐
             │                                             │
        succeeds                                    every provider down
             │                                             │
             ▼                                  ┌──────────┴───────────┐
        200 + result                     interactive              deferrable
                                              │                       │
                                              ▼                       ▼
                                         502 fail fast        202 + job id,
                                                              queued for retry
```

- **Interactive → 502.** A clear, immediate error. The caller decides what to do
  (show an error, retry itself, degrade).
- **Deferrable → 202 Accepted.** The request is put on a queue and the caller
  gets back a **job id** and a status URL:

  ```json
  {
    "job_id": "job-940afa3b…",
    "status": "queued",
    "status_url": "/v1/jobs/job-940afa3b…",
    "message": "providers unavailable; request queued for retry"
  }
  ```

Note the nuance: a deferrable request is only queued **if it can't be served now**.
When providers are healthy it's answered inline with a normal 200 — you get the
fast path when things are fine, and durability only when you need it.

---

## 4. How a deferrable request survives the outage

Queuing alone isn't enough — something has to actually retry the work. A
background **worker** ([`app/jobs.py`](../app/jobs.py)) does that:

1. The request is stored as a **Job** (`queued`) and put on the queue.
2. The worker picks it up (`running`) and calls the router.
3. If it still fails (providers down), the worker **waits and retries** with
   **exponential backoff** — 1s, 2s, 4s, … up to a cap. This is what carries the
   request across the outage without hammering the dead providers.
4. When a provider recovers, a retry **succeeds**: the job becomes `succeeded`
   and its result is stored (and its cost recorded, just like an inline call).
5. If it never recovers within `JOB_MAX_ATTEMPTS`, the job becomes `failed` with
   the last error.

### The job lifecycle

```
  QUEUED ──► RUNNING ──(a retry succeeds)──► SUCCEEDED
                │
                └──(all attempts exhausted)──► FAILED
```

### Polling a job

```
GET /v1/jobs/{job_id}
```

```json
{ "job_id": "job-940afa3b…", "status": "succeeded", "attempts": 2,
  "tenant": "acme", "feature": "batch", "request_class": "default",
  "result": { …the OpenAI-shaped completion… } }
```

`status` is `queued` → `running` → `succeeded`/`failed`. The `result` appears
once it succeeds; `error` appears if it fails.

---

## 5. Configuration

| Variable | Default | Meaning |
|----------|---------|---------|
| `JOB_MAX_ATTEMPTS` | 10 | How many times a deferred job is retried before giving up |
| `JOB_BACKOFF_BASE_S` | 1.0 | First backoff delay; doubles each retry |
| `JOB_BACKOFF_CAP_S` | 30 | Maximum backoff delay |

---

## 6. Testing & verification

The worker takes an injectable sleep, so the backoff loop runs instantly in
tests.

```bash
python -m pytest tests/ -q      # 34 passed
```

- **`tests/test_jobs.py`** — a deferred job survives an outage and succeeds after
  retries (with backoff `[1, 2, 4]`); fails after max attempts; fires its
  success callback.
- **`tests/test_api_modes.py`** — interactive succeeds when healthy; interactive
  fails fast (502) on outage; deferrable is queued (202) on outage and pollable;
  deferrable is served inline (200) when healthy; unknown job id → 404.

Live end-to-end (outage → recovery):

```
OUTAGE: all providers down
  interactive -> HTTP 502 (all_providers_failed)      [fail fast]
  deferrable  -> HTTP 202 job=job-940afa3b status=queued  [queued, survives]
RECOVERY: providers back up
  polled job  -> status=succeeded attempts=2
  result served_by=ollama
```

---

## 7. What this step does — and doesn't — do yet

**Does:** a deferrable request survives a **provider outage** — providers can be
down for a while and the work still completes once they recover.

**Doesn't (yet):** survive a **gateway restart**. The queue is in-process, so if
the gateway process itself crashes, queued (not-yet-succeeded) jobs are lost. The
`JobQueue` is written behind a small interface so a durable backend (e.g. Redis
or a database) can replace it next, making jobs restart-safe. Idempotency keys —
so a retried job can't double-charge or double-act — are the other planned
follow-up.

---

## 8. One-paragraph summary

Every request is classified at the API boundary as **interactive** (a human is
waiting — fail fast with 502 if it can't be served) or **deferrable** (no one is
blocked — survive the outage). A deferrable request that can't be served right now
is stored as a job and retried by a background worker with exponential backoff
until a provider recovers; the caller gets a 202 with a job id to poll, and the
job's result (and cost) land once it finally succeeds.
