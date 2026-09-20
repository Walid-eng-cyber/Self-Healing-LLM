"""
Per-request metadata: tenant, feature, request id.

Every billable request must carry enough context to attribute its cost. We take
that context from HTTP headers, so OpenAI clients stay compatible — they set
default headers once and change nothing else:

    X-Tenant-Id   who to bill / attribute usage to      (required)
    X-Feature     which product feature made the call   (required)
    X-Request-Id  trace id for this call                (optional; generated if absent)

tenant and feature are required because without them cost cannot be attributed.
request id is generated when the client does not supply one, and is always
echoed back in the X-Request-Id response header so callers can correlate.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from fastapi import Header


@dataclass(frozen=True)
class RequestMetadata:
    tenant: str
    feature: str
    request_id: str


class MissingMetadata(Exception):
    """Raised when a required metadata header is absent, mapped to HTTP 400."""

    def __init__(self, missing: list[str]) -> None:
        self.missing = missing
        super().__init__(f"missing required headers: {', '.join(missing)}")


def _clean(value: str | None) -> str:
    return (value or "").strip()


async def get_metadata(
    x_tenant_id: str | None = Header(default=None, alias="X-Tenant-Id"),
    x_feature: str | None = Header(default=None, alias="X-Feature"),
    x_request_id: str | None = Header(default=None, alias="X-Request-Id"),
) -> RequestMetadata:
    """FastAPI dependency: extract and validate request metadata."""
    tenant = _clean(x_tenant_id)
    feature = _clean(x_feature)

    missing = []
    if not tenant:
        missing.append("X-Tenant-Id")
    if not feature:
        missing.append("X-Feature")
    if missing:
        raise MissingMetadata(missing)

    # Accept a client-supplied trace id, otherwise mint one.
    request_id = _clean(x_request_id) or f"req-{uuid.uuid4().hex}"

    return RequestMetadata(tenant=tenant, feature=feature, request_id=request_id)
