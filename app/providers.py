"""
Provider abstraction, powered by LiteLLM.

LiteLLM normalizes the differences between vendors: OpenAI, Anthropic, Gemini
and 100+ others are all called through one `litellm.acompletion(...)` and all
return the same OpenAI-shaped object. So a single `LiteLLMProvider` class serves
every vendor — we only change the model string.

  * LiteLLMProvider - one configured upstream (a name + a LiteLLM model string).
                      Runs against a real API when a key is present, or in
                      LiteLLM "mock" mode (no network) when it is not.
  * RouterProvider  - holds the pool and fails over: tries each provider in
                      order and returns the first success. This is where the
                      "self-healing" begins.
"""

from __future__ import annotations

import time

import litellm

from .circuit_breaker import CircuitBreaker
from .config import PROVIDER_SPECS, ProviderSpec, settings
from .schemas import ChatCompletionRequest, ChatCompletionResponse

# Keep LiteLLM quiet; we do our own error handling and logging.
litellm.suppress_debug_info = True
litellm.drop_params = True  # silently drop params a given vendor doesn't support


# OpenAI chat params we forward to LiteLLM when the client sets them.
_FORWARDED_PARAMS = (
    "temperature",
    "top_p",
    "max_tokens",
    "stop",
    "n",
    "presence_penalty",
    "frequency_penalty",
    "user",
)


class ProviderError(Exception):
    """A provider could not serve this request, so the router may try another."""


class Provider:
    name: str = "base"

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        raise NotImplementedError


class LiteLLMProvider(Provider):
    """
    One upstream, called through LiteLLM, guarded by a circuit breaker.

    `complete()` consults the breaker before calling (fail fast when the circuit
    is open) and records the outcome + latency afterwards, so the breaker can
    trip and recover automatically. `healthy` remains a manual kill-switch for
    maintenance / demos, checked before the breaker.
    """

    def __init__(
        self,
        name: str,
        model: str,
        api_key: str | None,
        mock: bool,
        timeout: float,
        api_base: str | None = None,
        breaker: CircuitBreaker | None = None,
    ) -> None:
        self.name = name
        self.model = model
        self.api_key = api_key
        self.mock = mock
        self.timeout = timeout
        self.api_base = api_base
        self.healthy = True
        self.breaker = breaker or CircuitBreaker(name, **settings.breaker_kwargs())

    @classmethod
    def from_spec(cls, spec: ProviderSpec) -> "LiteLLMProvider":
        return cls(
            name=spec.name,
            model=spec.model,
            api_key=settings.key_for(spec),
            mock=settings.is_mock(spec),
            timeout=settings.UPSTREAM_TIMEOUT,
            api_base=spec.api_base,
            breaker=CircuitBreaker(spec.name, **settings.breaker_kwargs()),
        )

    def _params(self, request: ChatCompletionRequest) -> dict:
        params = {}
        for field in _FORWARDED_PARAMS:
            value = getattr(request, field, None)
            if value is not None:
                params[field] = value
        return params

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        # 1) Manual kill-switch (maintenance / demo).
        if not self.healthy:
            raise ProviderError(f"{self.name}: marked unhealthy")

        # 2) Circuit breaker: skip immediately if the circuit is open.
        if not self.breaker.allow():
            raise ProviderError(f"{self.name}: circuit {self.breaker.state.value}")

        # 3) Make the call, timing it, and record the outcome for the breaker.
        start = time.perf_counter()
        try:
            result = await self._invoke(request)
        except ProviderError:
            self.breaker.record(ok=False, latency_ms=(time.perf_counter() - start) * 1000)
            raise
        self.breaker.record(ok=True, latency_ms=(time.perf_counter() - start) * 1000)
        return result

    async def _invoke(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        """The raw LiteLLM call, with no breaker logic."""
        messages = [m.model_dump(exclude_none=True) for m in request.messages]

        # In mock mode, LiteLLM returns this canned string without any network
        # call. We make it name the provider so failover is visible in the reply.
        mock_response = None
        if self.mock:
            last_user = next(
                (m.get("content") for m in reversed(messages) if m.get("role") == "user"),
                "",
            )
            mock_response = f"[{self.name}:{self.model}] You said: {last_user}"

        try:
            resp = await litellm.acompletion(
                model=self.model,
                messages=messages,
                api_key=self.api_key,
                api_base=self.api_base,  # e.g. a local Ollama server; None otherwise
                timeout=self.timeout,
                mock_response=mock_response,
                **self._params(request),
            )
        except Exception as exc:  # LiteLLM raises many exception types
            raise ProviderError(f"{self.name}: {type(exc).__name__}: {exc}") from exc

        # LiteLLM's ModelResponse is already OpenAI-shaped; normalize into ours.
        data = resp.model_dump()
        data.setdefault("model", self.model)
        result = ChatCompletionResponse.model_validate(data)
        result.served_by = self.name
        return result


class RouterProvider(Provider):
    """
    Fails over across the pool: try each provider in order, return the first
    success. If every provider fails, raise a combined ProviderError.
    """

    name = "router"

    def __init__(self, pool: list[LiteLLMProvider]) -> None:
        if not pool:
            raise ValueError("RouterProvider needs at least one provider")
        self.pool = pool

    async def complete(self, request: ChatCompletionRequest) -> ChatCompletionResponse:
        errors: list[str] = []
        for provider in self.pool:
            try:
                return await provider.complete(request)
            except ProviderError as exc:
                # This upstream failed; record why and fall through to the next.
                errors.append(str(exc))
                continue
        raise ProviderError("all providers failed -> " + " | ".join(errors))


def build_pool() -> list[LiteLLMProvider]:
    """Build the provider pool from configuration (at least three providers)."""
    return [LiteLLMProvider.from_spec(spec) for spec in PROVIDER_SPECS]


def build_router() -> RouterProvider:
    return RouterProvider(build_pool())
