# Architecture

This document explains how the gateway works and how the design leaves room for
the later phases. If you only read one file to understand the code, read this one.

## System overview

![Self-healing LLM gateway request flow](architecture.svg)

A request flows top to bottom: the client (an OpenAI SDK pointed at the gateway)
sends a call with tenant/feature headers; the **metadata gate** rejects it with
400 if those are missing; the **router** tries the **provider pool** in order,
failing over between providers that LiteLLM normalizes; a **cost record** is
logged; and an OpenAI-shaped **response** returns with the request-id echoed.
Each provider in the pool is guarded by its own **circuit breaker** (closed →
open → half-open), shown in the legend. The sections below describe each part.

## Phase 2 update — LiteLLM provider pool + failover

The single provider from Phase 1 is now a **pool of three**, fronted by a
`RouterProvider` that fails over between them.

- **LiteLLM normalizes vendors.** OpenAI, Anthropic and Gemini are all called
  through one `litellm.acompletion(...)` and all return the same OpenAI-shaped
  object. So one `LiteLLMProvider` class serves every vendor — only the model
  string changes (see `PROVIDER_SPECS` in `config.py`).
- **Three providers are configured**, so failover always has a next hop:
  `openai → anthropic → gemini`. Order is the pool order in `config.py`.
- **`RouterProvider.complete()`** tries each provider in turn and returns the
  first success; if all fail it raises a combined error (HTTP 502).
- **No keys required.** Any provider without an API key runs in LiteLLM *mock*
  mode (a deterministic, self-naming reply, no network), so the gateway starts
  and failover is demoable offline. Each provider has a `healthy` flag you can
  flip to simulate an outage today; Phase 2's health checks will drive it
  automatically.
- **`GET /health`** now returns every provider's `name`, `model`, `mode`
  (mock/live) and `healthy` state — the feed the future dashboard reads.

## Required metadata + cost attribution

Every call to `/v1/chat/completions` must carry attribution metadata, passed as
HTTP headers so OpenAI clients stay compatible (they set default headers once):

| Header | Required | Meaning |
|--------|----------|---------|
| `X-Tenant-Id` | yes | who to attribute usage/cost to |
| `X-Feature` | yes | which product feature made the call |
| `X-Request-Id` | no | trace id; generated if absent, always echoed back |

- **Enforcement** (`metadata.py`): a FastAPI dependency reads the headers. If
  `X-Tenant-Id` or `X-Feature` is missing, the request is rejected with **400**
  in the OpenAI error envelope (listing the missing headers). tenant and feature
  are required because cost cannot be attributed without them.
- **Request id**: taken from `X-Request-Id` if present, otherwise minted
  (`req-<uuid>`), and always returned in the `X-Request-Id` response header — on
  both the success and failure paths — so callers can correlate.
- **Attribution** (`accounting.py`): after a successful completion the gateway
  emits one structured usage record — tenant, feature, request_id, model,
  served_by, token counts, and an **estimated cost** from the `PRICES` table in
  `config.py`. This is the half of a gateway's value that the metadata unlocks:
  "which tenant / which feature spent what". A later phase feeds the same records
  to Prometheus and the dashboard.

Example usage record (one JSON log line per request):

```json
{"request_id": "req-…", "tenant": "acme", "feature": "search",
 "model": "gpt-4o-mini", "served_by": "openai",
 "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
 "estimated_cost_usd": 1.3e-05}
```

The rest of this document describes the Phase 1 foundation the above builds on.

## Goal of Phase 1

> Route every model call through one service, using an OpenAI-compatible request
> shape so existing clients adopt it by changing only their `base_url`.

Everything below serves that one sentence.

## The big picture

```
                                   ┌──────────────────────────────┐
  OpenAI client                    │      Gateway (FastAPI)        │
  (unchanged except base_url)      │                              │
        │                          │   main.py                    │
        │  POST /v1/chat/completions   ├─ validates with schemas.py │
        ├─────────────────────────────►│                          │
        │                          │   └─ calls a Provider ───────┼──► upstream
        │  OpenAI-shaped JSON          │        (providers.py)     │    (mock, or a
        │◄─────────────────────────────┤                          │     real OpenAI-
        │                          │                              │     compatible API)
        └──                          └──────────────────────────────┘
```

The client thinks it is talking to OpenAI. It is actually talking to the
gateway, which owns the decision of *where* the request really goes. In Phase 1
that decision is trivial (one provider). The value is that the decision now
lives in **one place** — which is what makes health checks, failover, caching,
and metrics possible later without touching client code.

## Request lifecycle (step by step)

A single `POST /v1/chat/completions` travels like this:

1. **Arrival.** FastAPI receives the HTTP request in `main.py`.
2. **Validation.** FastAPI parses the JSON body into a
   `ChatCompletionRequest` (`schemas.py`). If `model` or `messages` is missing
   or malformed, FastAPI rejects it automatically with a 422 — we write no
   validation code. Unknown OpenAI fields (`tools`, `seed`, `response_format`,
   …) are preserved because the model allows extras.
3. **Routing.** The handler calls `provider.complete(request)`. `provider` is a
   single object chosen at startup by `build_default_provider()`.
4. **Upstream call.** The provider produces an OpenAI-shaped
   `ChatCompletionResponse`:
   - `MockProvider` builds a canned reply locally (no network).
   - `OpenAIProvider` forwards the body to a real upstream via `httpx` and
     validates the reply back into our schema.
5. **Failure path.** If the provider raises `ProviderError`, the handler returns
   a 502 wrapped in OpenAI's `{"error": {...}}` envelope. (Phase 2 turns this
   single failure into a *failover* to the next provider instead of an error.)
6. **Response.** FastAPI serializes the `ChatCompletionResponse` to JSON and
   sends it back. It includes a non-standard `served_by` field naming which
   provider answered; OpenAI clients ignore unknown fields, so this is safe.

## The modules

| File | Responsibility | Why it is separate |
|------|----------------|--------------------|
| `app/main.py` | HTTP layer: routes, error envelope, wiring | Keep transport concerns out of business logic |
| `app/schemas.py` | The OpenAI request/response contract | One place defines "what OpenAI-compatible means" here |
| `app/providers.py` | How a request becomes a response | The seam where failover, retries, and new vendors plug in |
| `app/config.py` | Environment-driven settings | No secrets or knobs hard-coded in code |

### `schemas.py` — the contract

Two ideas make it genuinely OpenAI-compatible:

- **Required fields match OpenAI.** Only `model` and `messages` are required;
  everything else is optional, exactly like OpenAI.
- **`extra = "allow"`.** Any field we did not explicitly model is kept and
  passed through, so clients using newer OpenAI features are not blocked by us.

The response models (`ChatCompletionResponse`, `Choice`, `Usage`, …) reproduce
OpenAI's structure — `id`, `object`, `created`, `model`, `choices[]`, `usage` —
so client SDKs parse it without special-casing.

### `providers.py` — the seam

`Provider` is a tiny interface:

```python
class Provider:
    name: str
    async def complete(self, request) -> ChatCompletionResponse: ...
```

Because `main.py` depends only on this interface, later phases add behavior
*behind* it without changing the API:

- **Phase 2** introduces a `RouterProvider` that holds a list of providers,
  checks their health, and calls the next healthy one on failure. `main.py`
  still just calls `provider.complete(...)`.
- New vendors are new `Provider` subclasses; nothing else changes.

`ProviderError` is the explicit "this upstream failed, you may try another"
signal — the hook failover will use.

### `config.py` — the knobs

Settings come from environment variables so the same code runs locally (mock)
and in production (real keys) with no edits:

| Variable | Default | Meaning |
|----------|---------|---------|
| `GATEWAY_DEFAULT_PROVIDER` | `mock` | `mock` (no key) or `openai` |
| `OPENAI_API_KEY` | — | Key for a real upstream |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Any OpenAI-compatible base URL |
| `GATEWAY_UPSTREAM_TIMEOUT` | `30` | Seconds before an upstream call fails |

## Why the mock provider exists

The mock provider is not throwaway scaffolding — it is a design choice:

- The service **runs with zero credentials**, so you can develop and demo
  without spending money.
- It returns a **deterministic** reply, which makes tests and the failover demo
  predictable (you know exactly what a "healthy" provider should say).
- In Phase 2 you can run two mock providers and kill one on stage to show
  rerouting, all offline.

## API reference

### `POST /v1/chat/completions`

Request (OpenAI chat-completions shape):

```json
{
  "model": "gpt-4",
  "messages": [
    {"role": "system", "content": "You are helpful."},
    {"role": "user", "content": "Hello gateway!"}
  ],
  "temperature": 0.7
}
```

Response:

```json
{
  "id": "chatcmpl-…",
  "object": "chat.completion",
  "created": 1789804954,
  "model": "gpt-4",
  "choices": [
    {"index": 0,
     "message": {"role": "assistant", "content": "…"},
     "finish_reason": "stop"}
  ],
  "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
  "served_by": "mock"
}
```

Errors use OpenAI's envelope:

```json
{"error": {"message": "…", "type": "upstream_error", "code": "provider_failed"}}
```

### Other endpoints

| Method | Path | Purpose |
|--------|------|---------|
| GET | `/` | Service info (phase, default provider, endpoint list) |
| GET | `/health` | Liveness check; returns the active provider's name |
| GET | `/v1/models` | Minimal model list so client setup calls don't error |
| GET | `/docs` | Interactive Swagger UI |

## Verifying it works

With the real OpenAI SDK — the only change is `base_url`:

```python
from openai import OpenAI
client = OpenAI(base_url="http://127.0.0.1:8000/v1", api_key="not-needed-for-mock")
resp = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Does the real SDK work?"}],
)
print(resp.choices[0].message.content)
```

If that prints a reply, Phase 1 is doing its job: a real client adopted the
gateway without code changes.

## What Phase 1 deliberately does **not** do

These are out of scope now and land in later phases:

- **Streaming** (`stream: true`) — Phase 1 forces non-streaming.
- **Multiple providers / failover** — one provider only. (Phase 2)
- **Caching / rate limiting** — no Redis yet. (Phase 3)
- **Metrics / dashboard** — no Prometheus/Grafana yet. (Phase 4)

Naming them here keeps the seams visible and the scope honest.
