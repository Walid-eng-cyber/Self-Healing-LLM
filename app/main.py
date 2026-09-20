"""
Self-Healing LLM Gateway.

A FastAPI service that exposes an OpenAI-compatible /v1/chat/completions
endpoint. Existing OpenAI clients adopt it by changing only their base_url;
every model call flows through this one service, which fails over across a
LiteLLM-backed provider pool and attributes each call's cost to a tenant and
feature via required request metadata.
"""

from __future__ import annotations

import logging

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .accounting import record_usage
from .metadata import MissingMetadata, RequestMetadata, get_metadata
from .providers import ProviderError, build_router
from .schemas import ChatCompletionRequest, ChatCompletionResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

app = FastAPI(
    title="Self-Healing LLM Gateway",
    version="0.3.0",
    description="OpenAI-compatible gateway: LiteLLM provider pool, failover, and per-tenant cost attribution.",
)

# A router over a pool of providers (at least three). It fails over between
# them, so a single provider outage does not take the gateway down.
router = build_router()


@app.get("/")
def root():
    """Human-friendly landing info."""
    return {
        "service": "self-healing-llm-gateway",
        "phase": 2,
        "providers": [p.name for p in router.pool],
        "request_classes": router.preference_lists,
        "endpoints": ["/v1/chat/completions", "/v1/models", "/health", "/docs"],
    }


@app.get("/health")
def health():
    """
    Liveness plus the state of every provider in the pool. This is what the
    future dashboard reads to show who is up and who is down.
    """
    return {
        "status": "ok",
        "providers": [
            {
                "name": p.name,
                "model": p.model,
                "mode": "mock" if p.mock else "live",
                "healthy": p.healthy,
                "circuit": p.breaker.stats(),
            }
            for p in router.pool
        ],
    }


@app.get("/v1/models")
def list_models():
    """
    Minimal OpenAI-compatible model list so clients that call /v1/models
    during setup don't error. Real model routing arrives in a later phase.
    """
    return {
        "object": "list",
        "data": [
            {"id": "gpt-3.5-turbo", "object": "model", "owned_by": "gateway"},
            {"id": "gpt-4", "object": "model", "owned_by": "gateway"},
        ],
    }


@app.post("/v1/chat/completions", response_model=ChatCompletionResponse)
async def chat_completions(
    request: ChatCompletionRequest,
    response: Response,
    meta: RequestMetadata = Depends(get_metadata),
):
    """
    The main entry point. Requires X-Tenant-Id and X-Feature headers (and
    optionally X-Request-Id) so every call is attributable. Accepts the OpenAI
    chat-completions request shape, routes through the failover pool, records
    the usage/cost, and returns the OpenAI response shape.
    """
    # Always echo the trace id, even on the failure path.
    response.headers["X-Request-Id"] = meta.request_id

    try:
        result = await router.complete(request, request_class=meta.request_class)
    except ProviderError as exc:
        # Reached only when EVERY provider in the pool has failed.
        return JSONResponse(
            status_code=502,
            headers={"X-Request-Id": meta.request_id},
            content={
                "error": {
                    "message": str(exc),
                    "type": "upstream_error",
                    "code": "all_providers_failed",
                }
            },
        )

    # The payoff: attribute this call's cost to the tenant + feature.
    record_usage(meta, result.model, result.served_by or "unknown", result.usage)
    return result


@app.exception_handler(MissingMetadata)
async def missing_metadata_handler(request: Request, exc: MissingMetadata):
    """A required metadata header was absent -> 400 in the OpenAI envelope."""
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": str(exc),
                "type": "invalid_request_error",
                "code": "missing_metadata",
                "missing": exc.missing,
            }
        },
    )


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Return errors in OpenAI's {"error": {...}} envelope."""
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": f"Internal gateway error: {exc}",
                "type": "gateway_error",
                "code": "internal_error",
            }
        },
    )
