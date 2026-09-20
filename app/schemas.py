"""
OpenAI-compatible request and response shapes.

These mirror the fields of OpenAI's /v1/chat/completions endpoint so that
existing OpenAI client libraries can point at this gateway by only changing
their base_url. We deliberately keep the important fields and allow extras to
pass through, rather than trying to model every optional parameter.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Request shape (what clients send us)
# ---------------------------------------------------------------------------

class ChatMessage(BaseModel):
    """A single message in the conversation."""

    role: Literal["system", "user", "assistant", "tool", "function"]
    content: Optional[str] = None
    name: Optional[str] = None

    # Allow tool_calls / function_call / etc. to ride along untouched.
    model_config = {"extra": "allow"}


class ChatCompletionRequest(BaseModel):
    """
    The body of POST /v1/chat/completions.

    Only `model` and `messages` are required, matching OpenAI. Everything else
    is optional and forwarded to the upstream provider as-is.
    """

    model: str
    messages: list[ChatMessage]

    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    stop: Optional[Any] = None
    n: Optional[int] = None
    presence_penalty: Optional[float] = None
    frequency_penalty: Optional[float] = None
    user: Optional[str] = None

    # Any other OpenAI parameter (tools, response_format, seed, ...) is kept.
    model_config = {"extra": "allow"}


# ---------------------------------------------------------------------------
# Response shape (what we send back)
# ---------------------------------------------------------------------------

class ResponseMessage(BaseModel):
    role: str = "assistant"
    content: Optional[str] = None
    model_config = {"extra": "allow"}


class Choice(BaseModel):
    index: int = 0
    message: ResponseMessage
    finish_reason: Optional[str] = "stop"


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ChatCompletionResponse(BaseModel):
    """A non-streaming chat completion response, OpenAI-shaped."""

    id: str = Field(default_factory=lambda: f"chatcmpl-{uuid.uuid4().hex}")
    object: str = "chat.completion"
    created: int = Field(default_factory=lambda: int(time.time()))
    model: str
    choices: list[Choice]
    usage: Usage = Field(default_factory=Usage)

    # Gateway-specific metadata: which upstream actually served the request.
    # Non-standard, so OpenAI clients simply ignore it.
    served_by: Optional[str] = None


# ---------------------------------------------------------------------------
# Error shape (OpenAI returns errors wrapped in {"error": {...}})
# ---------------------------------------------------------------------------

class ErrorBody(BaseModel):
    message: str
    type: str = "gateway_error"
    code: Optional[str] = None


class ErrorResponse(BaseModel):
    error: ErrorBody
