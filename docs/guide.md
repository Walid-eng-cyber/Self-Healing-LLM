# The Gateway, Explained

A plain-language walkthrough of everything the gateway does so far. Read this
top to bottom and you will understand the whole thing: what it is, why each
piece exists, and exactly what happens to a request as it flows through.

If you want the terse reference instead, see [architecture.md](architecture.md).

---

## 1. What is this thing, in one picture

Your app normally calls an AI provider directly:

```
  your app  ───────────────►  OpenAI
```

If OpenAI is slow, down, or expensive, your app feels it directly, and you have
no idea which part of your app spent what.

The gateway sits in the middle and takes over those decisions:

```
  your app  ──►  ┌─────────── GATEWAY ───────────┐  ──►  OpenAI
                 │  • speaks OpenAI's language     │  ──►  Anthropic (Claude)
                 │  • picks a healthy provider     │  ──►  Google (Gemini)
                 │  • reroutes if one fails        │
                 │  • records who spent what       │
                 └─────────────────────────────────┘
```

Your app still thinks it is talking to OpenAI. It is actually talking to the
gateway, which now owns three jobs:

1. **Route** every model call through one place (so the other two jobs are even
   possible).
2. **Fail over** when a provider breaks, across a pool of at least three.
3. **Attribute cost** — tie every call to a tenant and a feature.

The rest of this document explains each job.

---

## 2. Job 1 — One door for every model call (OpenAI-compatible)

### The idea

Every AI vendor has a slightly different API. If your code is written for
OpenAI, switching or adding vendors means rewriting code. That is painful, so
teams avoid it — and then they are stuck with one vendor.

The gateway removes that pain by **speaking OpenAI's language itself**. Your app
keeps using its normal OpenAI client and only changes one setting: the
`base_url` (the web address it sends requests to).

```python
from openai import OpenAI

# The ONLY change: point base_url at the gateway.
client = OpenAI(
    base_url="http://localhost:8000/v1",
    api_key="not-needed-for-mock",
    default_headers={"X-Tenant-Id": "acme", "X-Feature": "search"},  # explained in Job 3
)

resp = client.chat.completions.create(
    model="gpt-4",
    messages=[{"role": "user", "content": "Hello!"}],
)
print(resp.choices[0].message.content)
```

That is the whole adoption story: **change the address, keep everything else.**
This works because the gateway accepts the exact request shape OpenAI accepts
and returns the exact response shape OpenAI returns.

### Where this lives in the code

- **`app/schemas.py`** — defines "what an OpenAI request/response looks like"
  using Pydantic models. Two tricks make it truly compatible:
  - Only `model` and `messages` are required, exactly like OpenAI.
  - `extra = "allow"` means any field we did not explicitly list (new OpenAI
    features like `tools` or `seed`) is kept and passed through, not rejected.
- **`app/main.py`** — the web server. It exposes the endpoints:

  | Method | Path | What it's for |
  |--------|------|---------------|
  | POST | `/v1/chat/completions` | the main endpoint — send a chat, get a reply |
  | GET | `/health` | is the gateway up, and what's each provider's status |
  | GET | `/v1/models` | a minimal model list so client setup doesn't error |
  | GET | `/` | basic service info |
  | GET | `/docs` | an interactive test page (free from FastAPI) |

---

## 3. Job 2 — A pool of providers, with failover

### The idea

One provider is a single point of failure. So the gateway keeps a **pool** of
providers and tries them **in order** until one works:

```
   request ──►  ollama    (try first — local open-source model, free)
                  │  fails?
                  ▼
                openai    (try next)
                  │  fails?
                  ▼
                anthropic (try next)
                  │  fails?
                  ▼
                gemini    (last resort)
                  │  fails?
                  ▼
                return an error (everyone is down)
```

Because there are several providers, a failure always has somewhere to go.
That is the beginning of "self-healing": one provider dying does not take your
app down.

### LiteLLM — the universal adapter

Here is the clever part. Rather than write separate code for OpenAI, Anthropic,
and Gemini (each has a different API), the gateway uses **LiteLLM**, a library
that talks to all of them with one identical call. You only change the model
name:

```
litellm.acompletion(model="openai/gpt-4o-mini",      messages=...)
litellm.acompletion(model="anthropic/claude-3-5-...", messages=...)
litellm.acompletion(model="gemini/gemini-1.5-flash",  messages=...)
```

Same call, different model string. LiteLLM handles each vendor's quirks and
returns everything in the same OpenAI shape. So the gateway needs **one**
provider class, not three.

### Where this lives in the code

- **`app/config.py`** — the pool definition. This is the list you edit to add
  or reorder providers:

  ```python
  PROVIDER_SPECS = [
      ProviderSpec(name="ollama",    model="ollama_chat/llama3.1:8b",
                   api_base="http://localhost:11434", requires_key=False),   # local, free
      ProviderSpec(name="openai",    model="openai/gpt-4o-mini",                 api_key_env="OPENAI_API_KEY"),
      ProviderSpec(name="anthropic", model="anthropic/claude-3-5-sonnet-20241022", api_key_env="ANTHROPIC_API_KEY"),
      ProviderSpec(name="gemini",    model="gemini/gemini-1.5-flash",            api_key_env="GEMINI_API_KEY"),
  ]
  ```

  > **Note on the `provider/` prefix.** The model strings start with
  > `openai/`, `anthropic/`, `gemini/` on purpose. A bare `claude-...` string
  > confused LiteLLM ("LLM Provider NOT provided") — the explicit prefix tells
  > it exactly which vendor to use. This was a real bug caught by testing
  > failover step by step.

- **`app/providers.py`** — two classes:
  - **`LiteLLMProvider`** — one configured upstream. It has a `healthy` flag
    (True/False). When you call it, it asks LiteLLM for a completion and
    normalizes the reply into our OpenAI shape, stamping which provider answered
    (`served_by`).
  - **`RouterProvider`** — the failover engine. It holds the pool and loops
    through it: return the first success; if all fail, raise a combined error.

### A real open-source model (Ollama)

Not every provider is a paid cloud API. The pool's **first** provider is
`ollama`, a local open-source model (Llama 3.1 8B by default) served by
[Ollama](https://ollama.com) on your own machine. It differs from the cloud
providers in three ways:

- **No API key** — it just needs an Ollama server running. Instead of a key, its
  spec carries an `api_base` (`http://localhost:11434`). Keyless providers are
  always treated as *live*, never mock.
- **Free** — its price in the `PRICES` table is `0`, so its usage records show
  `estimated_cost_usd: 0.0`. Local compute costs you nothing per token.
- **First in line** — so a normal request returns a real, free answer by
  default; the paid cloud providers are the fallbacks behind it.

So a normal chat request now returns a genuine Llama answer:

```
served_by : ollama
model     : ollama_chat/llama3.1:8b
reply     : "An LLM gateway is a software interface that ..."
```

Break ollama (or stop the Ollama server) and the request falls through to the
next provider in the pool — real-to-fallback failover. Change the model with the
`OLLAMA_MODEL` env var (any model you've pulled, e.g. `qwen2.5:7b`).

### Running with no API keys (the mock mode)

You can run and demo all of this **without paying for anything**. Any provider
that has no API key automatically runs in LiteLLM's **mock mode**: it returns a
canned, self-naming reply (`[openai:...] You said: ...`) without touching the
network. That is why the failover demo works offline — every provider is a real,
callable object; it just answers locally.

Set a real key in `.env` and that provider flips from `mock` to `live`
automatically. Check `/health` to see each one's mode.

### Seeing failover work

Each provider's `healthy` flag can be flipped to simulate an outage. With all
three healthy, the first (openai) answers. Break it, and anthropic answers.
Break that too, and gemini answers:

```
all healthy        -> openai
openai down        -> anthropic
openai + anthro down -> gemini
all down           -> error (expected)
```

The `healthy` flag is still there as a manual kill-switch, but failover no
longer depends on flipping it by hand. Each provider now has a **circuit
breaker** that detects trouble and reroutes on its own — see
[circuit-breaker.md](circuit-breaker.md). In short: if a provider's error rate or
p95 latency crosses a threshold, its breaker trips **open** and the router skips
it instantly; after a cooldown the breaker tests it with a few trial requests and
**closes** again once it recovers. That automatic trip-and-heal is what makes the
gateway "self-healing".

---

## 4. Job 3 — Required metadata and cost attribution

### The idea

A gateway that just forwards calls is only half useful. The other half is
answering: **"which tenant, and which feature, spent this money?"** You cannot
answer that unless every request tells you who it is for. So the gateway
**requires** that information on every request.

The information travels as HTTP **headers** (not in the message body), which
keeps the OpenAI shape untouched — the client sets them once and forgets:

| Header | Required? | Meaning |
|--------|-----------|---------|
| `X-Tenant-Id` | **yes** | who to bill / attribute usage to |
| `X-Feature` | **yes** | which product feature made the call |
| `X-Request-Id` | no | a trace id for this one call |

- **tenant and feature are required.** Miss either and the gateway rejects the
  request with **400 Bad Request** and tells you which header was missing. They
  are required because *cost cannot be attributed without them*.
- **request id is optional.** If the client sends one, the gateway uses it; if
  not, the gateway makes one up (`req-<random>`). Either way it is returned in
  the `X-Request-Id` response header so you can match a reply back to its
  request in your logs. (Requiring clients to generate their own trace id is
  brittle, so the gateway generates it for them — this is the standard pattern.)

### The payoff: a usage record per request

After every successful call, the gateway writes one structured line that ties
the metadata to what was actually used and what it cost:

```json
{
  "request_id": "req-14b9ff…",
  "tenant": "acme",
  "feature": "search",
  "model": "gpt-4o-mini",
  "served_by": "openai",
  "prompt_tokens": 10,
  "completion_tokens": 20,
  "total_tokens": 30,
  "estimated_cost_usd": 1.3e-05
}
```

This is the thing you could not get by calling OpenAI directly: a per-tenant,
per-feature record of spend. The cost is estimated from a small price table
(`PRICES` in `config.py`), so token counts become actual dollars.

### Where this lives in the code

- **`app/metadata.py`** — reads the three headers, rejects the request (400) if
  a required one is missing, and generates a request id when needed.
- **`app/accounting.py`** — takes the metadata + the token usage, looks up the
  price, and writes the usage record.
- **`app/config.py`** — the `PRICES` table (USD per million tokens per model).
- **`app/main.py`** — ties it together: the endpoint requires the metadata,
  echoes the request id, and calls the accounting record after a completion.

Those usage records are also exactly what a future dashboard
(Prometheus + Grafana) will read to chart spend per tenant.

---

## 5. The full journey of one request

Putting all three jobs together, here is everything that happens to a single
`POST /v1/chat/completions`:

```
1. Arrive        The gateway (FastAPI) receives the HTTP request.

2. Check metadata Read X-Tenant-Id, X-Feature, X-Request-Id.
                  • missing tenant or feature? -> stop, return 400.
                  • no request id?             -> generate one.

3. Validate body Parse the JSON into an OpenAI-shaped request.
                  • malformed? -> FastAPI returns 422 automatically.

4. Route + failover The RouterProvider tries the pool in order:
                  openai -> anthropic -> gemini.
                  • First success wins; its name is recorded as served_by.
                  • All fail? -> return 502 "all_providers_failed".

5. Attribute     Write the usage record: tenant, feature, request_id,
                  model, served_by, tokens, estimated cost.

6. Respond       Return the OpenAI-shaped reply, with the X-Request-Id
                  header set so the caller can correlate it.
```

Every arrow above is a real, tested behavior.

---

## 6. The files at a glance

```
app/
  main.py        The web server: endpoints, wiring, error handling.
  schemas.py     The OpenAI request/response shapes (the contract).
  providers.py   LiteLLMProvider (one upstream) + RouterProvider (failover).
  config.py      The provider pool, prices, and settings.
  metadata.py    Requires tenant/feature, handles request id.
  accounting.py  Turns usage into an attributed cost record.
```

Read them in this order and each builds on the last:
`schemas → providers → config → metadata → accounting → main`.

---

## 7. Run it yourself

```bash
pip install -r requirements.txt
python -m uvicorn app.main:app --port 8000 --reload
```

Then:

- Open `http://localhost:8000/docs` and click **Try it out** on
  `/v1/chat/completions`. (Add the `X-Tenant-Id` and `X-Feature` headers, or
  you'll get the 400 — which is the point.)
- Check `http://localhost:8000/health` to see all three providers and whether
  each is in `mock` or `live` mode.
- Watch the server's console: every successful call prints its usage record.

---

## 8. What's built vs. what's next

**Built and tested:**
- OpenAI-compatible routing through one service.
- LiteLLM-normalized pool of three providers with manual-flag failover.
- Runs offline via mock mode (no API keys needed).
- Required tenant/feature metadata, generated request ids, per-request cost
  attribution.

**Also built (Phase 2):**
- **Per-provider circuit breaker** (closed / open / half-open) that trips on
  error rate or p95 latency and recovers on its own — automatic, no manual flag.
  See [circuit-breaker.md](circuit-breaker.md).

**Next steps (not built yet):**
- **Redis** — cache identical answers (save money) and enforce rate limits.
- **Prometheus + Grafana** — the live dashboard where you crash a provider and
  watch traffic reroute in real time.
```
