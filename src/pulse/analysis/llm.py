"""Thin wrapper around the Anthropic SDK for structured calls (plan Phase 3, architecture §4.4).

Every call:
- uses the configured model with adaptive thinking and a per-call effort level;
- streams, and reads the result with `get_final_message()`, so long batches don't time out;
- asks for structured output with a JSON schema made from a pydantic model;
- sends server-side `fallbacks` (beta `server-side-fallback-2026-07-01`) when configured;
- checks `stop_reason` before reading any content (`refusal`, `max_tokens`);
- puts `cache_control` on the stable system prompt and on an optional stable prefix block;
- logs the (already scrubbed) request and the response to `runs/{week}/llm/`.

The schema is sent through `output_config.format` and the text is validated here, after the
`stop_reason` check, rather than by the SDK's `output_format=` helper. That way a truncated
or refused response is reported as such, not as a confusing validation error.
"""

from __future__ import annotations

import itertools
import json
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

import anthropic
from pydantic import BaseModel, ValidationError

from pulse.config import Effort, LlmConfig
from pulse.fileio import write_json

FALLBACK_BETA = "server-side-fallback-2026-07-01"
DEFAULT_MAX_TOKENS = 64_000

# List prices for claude-opus-5-5 (USD per million tokens, 2026-11). Used only for the
# cost estimate in manifest.json; a fallback model bills at its own rates.
PRICE_PER_MTOK = {"input": 4.00, "output": 20.00, "cache_read": 0.20, "cache_write": 5.00}

T = TypeVar("T", bound=BaseModel)


class LlmError(RuntimeError):
    """A model call failed. `kind` says why, for the CLI and the manifest."""

    def __init__(self, message: str, *, kind: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.kind = kind
        self.retryable = retryable


class LlmRefusal(LlmError):
    def __init__(self, message: str, *, category: str | None) -> None:
        super().__init__(message, kind="refusal")
        self.category = category


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    seconds: float = 0.0
    models: dict[str, int] = field(default_factory=dict)  # model that served -> calls

    def add(self, other: Usage) -> None:
        self.calls += other.calls
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.seconds += other.seconds
        for m, n in other.models.items():
            self.models[m] = self.models.get(m, 0) + n

    @property
    def estimated_usd(self) -> float:
        p = PRICE_PER_MTOK
        return round((self.input_tokens * p["input"] + self.output_tokens * p["output"]
                      + self.cache_read_tokens * p["cache_read"]
                      + self.cache_write_tokens * p["cache_write"]) / 1_000_000, 4)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["seconds"] = round(self.seconds, 1)
        d["estimated_usd"] = self.estimated_usd
        return d


class StructuredLlm(Protocol):
    """What the clustering code needs from a model. Tests supply a fake."""

    usage: Usage

    def parse(self, *, name: str, system: str, prefix: str | None, user: str,
              schema: type[T], effort: Effort, max_tokens: int = DEFAULT_MAX_TOKENS) -> T: ...


class LlmClient:
    """Structured calls to Claude. Thread-safe: Pass B runs batches concurrently."""

    def __init__(self, cfg: LlmConfig, *, log_dir: Path | None = None,
                 client: Any | None = None) -> None:
        self.cfg = cfg
        self.log_dir = log_dir
        self.usage = Usage()
        self._client = client if client is not None else anthropic.Anthropic()
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def parse(self, *, name: str, system: str, prefix: str | None, user: str,
              schema: type[T], effort: Effort, max_tokens: int = DEFAULT_MAX_TOKENS) -> T:
        content: list[dict[str, Any]] = []
        if prefix:
            # Stable across the calls of one stage (e.g. theme definitions in Pass B).
            content.append({"type": "text", "text": prefix,
                            "cache_control": {"type": "ephemeral"}})
        content.append({"type": "text", "text": user})
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "max_tokens": max_tokens,
            "thinking": {"type": "adaptive"},
            "output_config": {
                "effort": effort,
                "format": {"type": "json_schema",
                           "schema": anthropic.transform_schema(schema)},
            },
            "system": [{"type": "text", "text": system,
                        "cache_control": {"type": "ephemeral"}}],
            "messages": [{"role": "user", "content": content}],
        }
        if self.cfg.fallbacks:
            kwargs["fallbacks"] = self.cfg.fallbacks
            kwargs["betas"] = [FALLBACK_BETA]

        with self._lock:
            seq = next(self._seq)
        started = time.monotonic()
        try:
            with self._client.beta.messages.stream(**kwargs) as stream:
                message = stream.get_final_message()
        except anthropic.RateLimitError as e:
            raise LlmError(f"{name}: rate limited by the API ({e.status_code})",
                           kind="rate_limit", retryable=True) from e
        except anthropic.APIStatusError as e:
            raise LlmError(f"{name}: API error {e.status_code}: {e.message}",
                           kind="api_status", retryable=e.status_code >= 500) from e
        except anthropic.APIConnectionError as e:
            raise LlmError(f"{name}: could not reach the API: {e}",
                           kind="connection", retryable=True) from e
        except TypeError as e:
            # The SDK raises TypeError when it finds no credentials at all.
            if "authentication" not in str(e).lower():
                raise
            raise LlmError(f"{name}: no Anthropic credentials: set ANTHROPIC_API_KEY in .env "
                           "or log in with `ant auth login`", kind="auth") from e
        elapsed = time.monotonic() - started

        call_usage = _usage_of(message, elapsed)
        with self._lock:
            self.usage.add(call_usage)
        text = "".join(b.text for b in message.content if getattr(b, "type", None) == "text")
        self._log(seq, name, kwargs, message, text, call_usage)

        # Check why the model stopped before reading any content.
        if message.stop_reason == "refusal":
            details = getattr(message, "stop_details", None)
            category = getattr(details, "category", None) if details else None
            raise LlmRefusal(f"{name}: the model declined the request "
                             f"(category: {category or 'unknown'})", category=category)
        if message.stop_reason == "max_tokens":
            raise LlmError(f"{name}: the response hit max_tokens ({max_tokens}) and is "
                           "incomplete", kind="max_tokens")
        try:
            return schema.model_validate_json(text)
        except ValidationError as e:
            raise LlmError(f"{name}: the response did not match the schema: "
                           f"{e.error_count()} errors", kind="schema") from e

    def _log(self, seq: int, name: str, kwargs: dict[str, Any], message: Any, text: str,
             usage: Usage) -> None:
        if self.log_dir is None:
            return
        request = {k: v for k, v in kwargs.items() if k != "output_config"}
        request["effort"] = kwargs["output_config"]["effort"]
        try:
            parsed: Any = json.loads(text)
        except json.JSONDecodeError:
            parsed = text
        write_json(self.log_dir / f"{seq:02d}_{name}.json", {
            "request": request,
            "response": {
                "id": getattr(message, "id", None),
                "request_id": getattr(message, "_request_id", None),
                "model": getattr(message, "model", None),
                "stop_reason": message.stop_reason,
                "usage": usage.to_dict(),
                "output": parsed,
            },
        })


def _usage_of(message: Any, seconds: float) -> Usage:
    u = getattr(message, "usage", None)
    model = getattr(message, "model", None) or "unknown"
    return Usage(
        calls=1,
        input_tokens=getattr(u, "input_tokens", 0) or 0,
        output_tokens=getattr(u, "output_tokens", 0) or 0,
        cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        cache_write_tokens=getattr(u, "cache_creation_input_tokens", 0) or 0,
        seconds=seconds,
        models={model: 1},
    )


def build_llm(cfg: LlmConfig, *, log_dir: Path | None = None) -> StructuredLlm:
    """The real client. Credentials come from the environment (ANTHROPIC_API_KEY or an
    `ant auth login` profile). The CLI calls this through the module so tests can patch it."""
    return LlmClient(cfg, log_dir=log_dir)
