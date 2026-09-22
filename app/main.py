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
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import JSONResponse

from .accounting import record_usage
from .config import settings
from .jobs import Job, JobQueue
from .metadata import MissingMetadata, RequestMetadata, get_metadata
from .providers import ProviderError, build_router
from .schemas import ChatCompletionRequest, ChatCompletionResponse

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

# A router over a pool of providers (at least three). It fails over between
# them, so a single provider outage does not take the gateway down.
router = build_router()


def _record_job_usage(job: Job) -> None:
    """Attribute cost for a deferred request once its retry finally succeeds."""
    result = job.result
    if result is not None:
        record_usage(
            job.meta,
            result.model,
            result.served_by or "unknown",
            result.usage,
            hedged=bool(result.hedged),
        )


# The deferrable work queue and its background retry worker.
job_queue = JobQueue(
    router,
    max_attempts=settings.JOB_MAX_ATTEMPTS,
    backoff_base_s=settings.JOB_BACKOFF_BASE_S,
    backoff_cap_s=settings.JOB_BACKOFF_CAP_S,
    on_success=_record_job_usage,
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    job_queue.start()  # start the background retry worker
    try:
        yield
    finally:
        await job_queue.stop()


app = FastAPI(
    title="Self-Healing LLM Gateway",
    version="0.4.0",
    description="OpenAI-compatible gateway: failover, cost attribution, and a deferrable work queue.",
    lifespan=lifespan,
)


@app.get("/")
def root():
    """Human-friendly landing info."""
    return {
        "service": "self-healing-llm-gateway",
        "phase": 3,
        "providers": [p.name for p in router.pool],
        "request_classes": router.preference_lists,
        "hedged_classes_ms": router.hedge_ms,
        "request_modes": ["interactive", "deferrable"],
        "endpoints": [
            "/v1/chat/completions",
            "/v1/jobs/{job_id}",
            "/v1/models",
            "/health",
            "/docs",
        ],
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
    optionally X-Request-Id / X-Request-Class / X-Request-Mode). Routes through
    the failover pool and returns the OpenAI response shape.

    X-Request-Mode classifies the request at the API boundary:
      * interactive (default) - the caller is waiting, so on total failure it
        fails fast with 502.
      * deferrable            - the caller can wait, so on total failure the
        request is queued and retried in the background (survives an outage),
        and the caller gets 202 Accepted with a job id to poll.
    """
    # Always echo the trace id, even on the failure path.
    response.headers["X-Request-Id"] = meta.request_id

    try:
        result = await router.complete(request, request_class=meta.request_class)
    except ProviderError as exc:
        # Every provider is currently unavailable.
        if meta.mode == "deferrable":
            job = job_queue.submit(request, meta.request_class, meta)
            return JSONResponse(
                status_code=202,
                headers={"X-Request-Id": meta.request_id},
                content={
                    "job_id": job.id,
                    "status": job.status.value,
                    "status_url": f"/v1/jobs/{job.id}",
                    "message": "providers unavailable; request queued for retry",
                },
            )
        # interactive -> fail fast.
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
    record_usage(
        meta,
        result.model,
        result.served_by or "unknown",
        result.usage,
        hedged=bool(result.hedged),
    )
    return result


@app.get("/v1/jobs/{job_id}")
def get_job(job_id: str):
    """Poll a deferred request's status (and its result once it succeeds)."""
    job = job_queue.get(job_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"error": {"message": "job not found", "type": "not_found"}},
        )
    body = {
        "job_id": job.id,
        "status": job.status.value,
        "attempts": job.attempts,
        "tenant": job.meta.tenant,
        "feature": job.meta.feature,
        "request_class": job.request_class,
    }
    if job.status.value == "succeeded" and job.result is not None:
        body["result"] = job.result.model_dump()
    elif job.status.value == "failed":
        body["error"] = job.error
    return body


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
