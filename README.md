# Self-Healing LLM Gateway

An OpenAI-compatible gateway that routes every model call through one service.
When a provider fails, it reroutes traffic automatically and shows it on a live
dashboard.

## Roadmap

- **Phase 1 (done): Route every model call through one service.** A FastAPI
  service that speaks the OpenAI `/v1/chat/completions` request/response shape,
  so existing OpenAI clients adopt it by changing only their `base_url`.
- **Phase 2 (done): Self-healing resilience.** LiteLLM normalizes vendor
  differences; a pool of providers (a local Ollama model plus OpenAI, Anthropic,
  Gemini) fails over automatically. Each provider has a circuit breaker
  (closed / open / half-open) that trips on error rate or p95 latency and
  recovers on its own via half-open probes; failover follows per-request-class
  preference lists; and latency-sensitive classes can hedge. Full write-up in
  [docs/phase-2.md](docs/phase-2.md).
- Phase 3: Redis caching + rate limiting.
- Phase 4: Prometheus metrics + Grafana dashboard.

## Documentation

- **[docs/guide.md](docs/guide.md) — start here.** A plain-language walkthrough
  of the whole gateway: what it is, why each piece exists, and exactly what
  happens to a request. Read this to understand the project.
- [docs/architecture.md](docs/architecture.md) — the terse reference: request
  lifecycle, each module, and the full API reference.
- **[docs/phase-2.md](docs/phase-2.md)** — the complete Phase 2 write-up in one
  place: the circuit breaker, half-open probes, per-request-class failover, and
  hedged requests (with the cost trade-off), plus config and tests.

## Run it

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000 --reload
```

Then open http://localhost:8000/docs

By default it uses the built-in **mock** provider, so it runs with no API key.

## Use it from an existing OpenAI client

Point the client at the gateway; change nothing else.

Every request must carry attribution metadata as headers — set them once via
`default_headers` and change nothing else:

```python
from openai import OpenAI

client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="not-needed-for-mock",
    default_headers={"X-Tenant-Id": "acme", "X-Feature": "search"},
)

resp = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

Required headers: `X-Tenant-Id` and `X-Feature` (a request without them is
rejected with 400). `X-Request-Id` is optional — it's generated if absent and
always returned in the response's `X-Request-Id` header. Each call emits a
usage record tying tenant + feature to tokens and estimated cost.

## Use a real upstream

Copy `.env.example` to `.env`, set `GATEWAY_DEFAULT_PROVIDER=openai` and your
`OPENAI_API_KEY`, then restart. Any OpenAI-compatible base URL works
(OpenAI, Groq, Together, a local vLLM server, ...).

## Endpoints

| Method | Path                    | Purpose                          |
|--------|-------------------------|----------------------------------|
| POST   | `/v1/chat/completions`  | OpenAI-compatible chat endpoint  |
| GET    | `/v1/models`            | Minimal model list               |
| GET    | `/health`               | Liveness check                   |
| GET    | `/`                     | Service info                     |
| GET    | `/docs`                 | Interactive API docs             |
