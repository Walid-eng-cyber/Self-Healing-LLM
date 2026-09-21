"""
Cost attribution.

Turns each completed request into a structured usage record that ties spend to a
tenant and feature. This is the half of a gateway's value that the required
metadata unlocks: you can now answer "which tenant / which feature spent what".

For now records are emitted as one JSON log line per request. A later phase
feeds the same records to Prometheus (and the dashboard) and/or a database.
"""

from __future__ import annotations

import json
import logging

from .config import PRICES
from .metadata import RequestMetadata
from .schemas import Usage

logger = logging.getLogger("gateway.usage")


def _short_model(model: str) -> str:
    """Drop any 'provider/' prefix so it matches the PRICES table."""
    return model.split("/", 1)[-1]


def estimate_cost_usd(model: str, prompt_tokens: int, completion_tokens: int) -> float | None:
    """Estimate cost from token counts, or None if the model isn't priced."""
    price = PRICES.get(_short_model(model))
    if price is None:
        return None
    cost = (prompt_tokens / 1_000_000) * price["input"] + (
        completion_tokens / 1_000_000
    ) * price["output"]
    return round(cost, 6)


def record_usage(
    meta: RequestMetadata,
    model: str,
    served_by: str,
    usage: Usage,
    hedged: bool = False,
) -> dict:
    """Build, log, and return the usage record for one request."""
    winner_cost = estimate_cost_usd(
        model, usage.prompt_tokens, usage.completion_tokens
    )
    record = {
        "request_id": meta.request_id,
        "tenant": meta.tenant,
        "feature": meta.feature,
        "request_class": meta.request_class,
        "model": model,
        "served_by": served_by,
        "hedged": hedged,
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "estimated_cost_usd": winner_cost,
        # A hedged call also runs a loser we cancel, so real spend is higher.
        # We can't know the loser's exact tokens, so we flag ~2x as a guide.
        "estimated_cost_with_hedge_usd": (
            round(winner_cost * 2, 6) if hedged and winner_cost is not None else winner_cost
        ),
    }
    logger.info("usage %s", json.dumps(record))
    return record
