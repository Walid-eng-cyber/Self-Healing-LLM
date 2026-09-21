"""
Gateway configuration, read from environment variables.

Phase 2 introduces a *pool* of providers. LiteLLM normalizes the differences
between vendors, so each provider is just a name + a LiteLLM model string + the
env var that holds its key. Failover walks this list in order.

The gateway runs with NO keys: any provider whose key is missing (or all of
them, if GATEWAY_FORCE_MOCK=1) runs in LiteLLM "mock" mode, returning a
deterministic reply that names itself. That is what lets us demo failover
offline — there is always somewhere for traffic to go.
"""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderSpec:
    name: str                       # friendly id shown in responses and the dashboard
    model: str                      # LiteLLM model string, e.g. "openai/gpt-4o-mini"
    api_key_env: str = ""           # env var that holds this provider's key (blank = none)
    api_base: str | None = None     # explicit endpoint (e.g. a local Ollama server)
    requires_key: bool = True       # False for local/keyless providers like Ollama


# A local open-source model served by Ollama. It needs no API key — just a
# running Ollama server — so it is a genuinely "live" provider even offline, and
# a natural cheap fallback. Override the model/host via env if you like.
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
OLLAMA_BASE_URL = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")

# At least three providers, so failover always has a next hop to try.
# Order matters: the router tries them top to bottom. We put the local
# open-source model first so a normal request returns a real answer for free;
# the cloud providers are the fallbacks.
# Cloud models carry an explicit "provider/model" prefix so LiteLLM always knows
# which vendor to route to (a bare "claude-..." can be ambiguous).
PROVIDER_SPECS: list[ProviderSpec] = [
    ProviderSpec(name="ollama",    model=f"ollama_chat/{OLLAMA_MODEL}",
                 api_base=OLLAMA_BASE_URL, requires_key=False),
    ProviderSpec(name="openai",    model="openai/gpt-4o-mini",                 api_key_env="OPENAI_API_KEY"),
    ProviderSpec(name="anthropic", model="anthropic/claude-3-5-sonnet-20241022", api_key_env="ANTHROPIC_API_KEY"),
    ProviderSpec(name="gemini",    model="gemini/gemini-1.5-flash",            api_key_env="GEMINI_API_KEY"),
]


# --- Request classes -> provider preference lists ---
#
# Different kinds of request should fail over differently. A cheap, high-volume
# classification call prefers fast/cheap providers; a long-form generation call
# prefers stronger models and only falls back to the local one as a last resort.
# Each list is an ordered set of provider NAMES (from PROVIDER_SPECS). The router
# tries them in this order, skipping any whose circuit breaker is open.
DEFAULT_REQUEST_CLASS = "default"

_ALL_PROVIDERS = [spec.name for spec in PROVIDER_SPECS]

REQUEST_CLASSES: dict[str, list[str]] = {
    # cheap + fast first; big cloud model only if the others are down
    "classification": ["ollama", "gemini", "openai"],
    # quality first; local model is the last resort
    "generation": ["openai", "anthropic", "ollama"],
    # unspecified/unknown class -> the full pool in declared order
    DEFAULT_REQUEST_CLASS: _ALL_PROVIDERS,
}


# Approximate list prices in USD per 1,000,000 tokens, keyed by the short model
# name (the "provider/" prefix is stripped before lookup). Used to turn token
# counts into an attributable cost per request. Local models are free.
PRICES: dict[str, dict[str, float]] = {
    "gpt-4o-mini":                 {"input": 0.15, "output": 0.60},
    "claude-3-5-sonnet-20241022":  {"input": 3.00, "output": 15.00},
    "gemini-1.5-flash":            {"input": 0.075, "output": 0.30},
    OLLAMA_MODEL:                  {"input": 0.0, "output": 0.0},  # local = free
}


class Settings:
    # Force every provider into mock mode even if keys are present. Handy for
    # the offline failover demo.
    FORCE_MOCK: bool = os.getenv("GATEWAY_FORCE_MOCK", "0") == "1"

    # Seconds to wait on an upstream before treating it as failed.
    UPSTREAM_TIMEOUT: float = float(os.getenv("GATEWAY_UPSTREAM_TIMEOUT", "30"))

    # --- Circuit breaker (per provider) ---
    CB_WINDOW_SECONDS: float = float(os.getenv("CB_WINDOW_SECONDS", "30"))
    CB_MIN_REQUESTS: int = int(os.getenv("CB_MIN_REQUESTS", "5"))
    CB_ERROR_RATE_THRESHOLD: float = float(os.getenv("CB_ERROR_RATE_THRESHOLD", "0.5"))
    CB_P95_LATENCY_BUDGET_MS: float = float(os.getenv("CB_P95_LATENCY_BUDGET_MS", "2000"))
    CB_OPEN_COOLDOWN_SECONDS: float = float(os.getenv("CB_OPEN_COOLDOWN_SECONDS", "15"))
    CB_HALF_OPEN_MAX_CALLS: int = int(os.getenv("CB_HALF_OPEN_MAX_CALLS", "3"))
    CB_HALF_OPEN_SUCCESSES_TO_CLOSE: int = int(os.getenv("CB_HALF_OPEN_SUCCESSES_TO_CLOSE", "2"))

    def breaker_kwargs(self) -> dict:
        return {
            "window_seconds": self.CB_WINDOW_SECONDS,
            "min_requests": self.CB_MIN_REQUESTS,
            "error_rate_threshold": self.CB_ERROR_RATE_THRESHOLD,
            "p95_budget_ms": self.CB_P95_LATENCY_BUDGET_MS,
            "open_cooldown_seconds": self.CB_OPEN_COOLDOWN_SECONDS,
            "half_open_max_calls": self.CB_HALF_OPEN_MAX_CALLS,
            "half_open_successes_to_close": self.CB_HALF_OPEN_SUCCESSES_TO_CLOSE,
        }

    def key_for(self, spec: ProviderSpec) -> str | None:
        return os.getenv(spec.api_key_env) if spec.api_key_env else None

    def is_mock(self, spec: ProviderSpec) -> bool:
        """
        A provider runs in mock mode if mocking is forced, or if it needs an API
        key but none is set. Keyless providers (e.g. a local Ollama server) are
        always live.
        """
        if self.FORCE_MOCK:
            return True
        if not spec.requires_key:
            return False
        return not self.key_for(spec)


settings = Settings()
