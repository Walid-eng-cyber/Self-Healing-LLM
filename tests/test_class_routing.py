"""
Per-request-class failover routing.

Verifies that the router follows each request class's preference list, and that
when a provider's circuit breaker is open, failover moves to the next provider
*in that class's list* — so different classes fail over differently.
"""

from __future__ import annotations

import asyncio

from app.circuit_breaker import CircuitState
from app.config import DEFAULT_REQUEST_CLASS, REQUEST_CLASSES
from app.providers import LiteLLMProvider, RouterProvider
from app.schemas import ChatCompletionRequest, ChatMessage

PROVIDER_NAMES = ["ollama", "openai", "anthropic", "gemini"]


def make_router() -> RouterProvider:
    pool = [
        LiteLLMProvider(name, "openai/gpt-4o-mini", None, mock=True, timeout=5)
        for name in PROVIDER_NAMES
    ]
    return RouterProvider(pool, REQUEST_CLASSES, DEFAULT_REQUEST_CLASS)


def force_open(router: RouterProvider, name: str) -> None:
    """Trip a provider's breaker open so allow() fails fast."""
    breaker = router.by_name[name].breaker
    breaker.state = CircuitState.OPEN
    breaker._opened_at = breaker._clock()  # within cooldown -> stays open


def route(router: RouterProvider, request_class: str) -> str:
    req = ChatCompletionRequest(model="m", messages=[ChatMessage(role="user", content="hi")])
    result = asyncio.run(router.complete(req, request_class=request_class))
    return result.served_by


# --- preference order when everything is healthy ---------------------------

def test_classification_prefers_ollama_first():
    assert route(make_router(), "classification") == "ollama"


def test_generation_prefers_openai_first():
    assert route(make_router(), "generation") == "openai"


def test_default_class_uses_pool_order():
    assert route(make_router(), DEFAULT_REQUEST_CLASS) == "ollama"


# --- failover follows the class's list when a breaker opens -----------------

def test_classification_fails_over_to_gemini_when_ollama_open():
    router = make_router()
    force_open(router, "ollama")
    # classification list is [ollama, gemini, openai] -> next is gemini
    assert route(router, "classification") == "gemini"


def test_generation_fails_over_to_anthropic_when_openai_open():
    router = make_router()
    force_open(router, "openai")
    # generation list is [openai, anthropic, ollama] -> next is anthropic
    assert route(router, "generation") == "anthropic"


def test_same_outage_routes_differently_by_class():
    # One provider (ollama) is down. The two classes react differently:
    router1 = make_router()
    force_open(router1, "ollama")
    # classification skips ollama -> gemini
    assert route(router1, "classification") == "gemini"

    router2 = make_router()
    force_open(router2, "ollama")
    # generation never wanted ollama first anyway -> still openai
    assert route(router2, "generation") == "openai"


# --- edge cases -------------------------------------------------------------

def test_unknown_class_falls_back_to_default():
    # An unrecognized class uses the default preference list.
    assert route(make_router(), "totally-made-up") == "ollama"


def test_all_open_in_class_raises():
    router = make_router()
    for name in ("ollama", "gemini", "openai"):  # every provider in classification
        force_open(router, name)
    try:
        route(router, "classification")
        assert False, "expected failure when all providers in the class are open"
    except Exception as exc:
        assert "classification" in str(exc)


if __name__ == "__main__":
    import sys

    funcs = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in funcs:
        try:
            fn()
            print(f"PASS  {fn.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {fn.__name__}: {exc}")
    print(f"\n{len(funcs) - failed}/{len(funcs)} passed")
    sys.exit(1 if failed else 0)
