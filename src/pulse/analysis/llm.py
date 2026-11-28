"""Structured model calls behind one interface, `StructuredLlm.parse` (architecture §4.4).

Two providers, picked by `llm.provider` in pulse.yaml:

- **groq** (default, free): `GroqClient`. Strict JSON Schema output; a 429 is waited out
  using `retry-after`; each call's tokens and the rate-limit headers are logged.
- **anthropic** (paid, optional `[llm]` extra): `LlmClient`, the Claude client.

Both check why the model stopped before validating the output with pydantic, log the
(already scrubbed) request and the response to `runs/{week}/llm/`, and raise `LlmError` with
the same `kind`s. Only text that went through the PII scrub is ever passed in.

The Claude client, for every call:
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
import os
import threading
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from pulse.config import Effort, GroqConfig, LlmConfig
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
    provider: str = "anthropic"
    rate_limit_waits: int = 0          # 429s that were waited out (Groq)

    def add(self, other: Usage) -> None:
        if other.calls:
            self.provider = other.provider
        self.rate_limit_waits += other.rate_limit_waits
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
        if self.provider == "groq":
            return 0.0                 # the free tier
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
        self._client = client if client is not None else _anthropic().Anthropic()
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def parse(self, *, name: str, system: str, prefix: str | None, user: str,
              schema: type[T], effort: Effort, max_tokens: int = DEFAULT_MAX_TOKENS) -> T:
        anthropic = _anthropic()
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


def _anthropic():
    """Import the SDK only when the LLM engine is used: it is an optional extra (`[llm]`)."""
    try:
        import anthropic
    except ImportError as e:
        raise LlmError("the LLM engine needs the optional `anthropic` package: "
                       'pip install -e ".[llm]"', kind="missing_dependency") from e
    return anthropic


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


# ---------------------------------------------------------------------------- Groq

GROQ_KEY_ENV = "GROQ_API_KEY"
_RATE_HEADERS = ("x-ratelimit-limit-tokens", "x-ratelimit-remaining-tokens",
                 "x-ratelimit-reset-tokens", "x-ratelimit-limit-requests",
                 "x-ratelimit-remaining-requests", "x-ratelimit-reset-requests")


def strict_schema(schema: type[BaseModel]) -> dict[str, Any]:
    """The pydantic model's JSON Schema in the form Groq's strict mode needs: every object
    lists all of its properties as required and sets `additionalProperties: false`."""

    def walk(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: walk(v) for k, v in node.items() if k != "default"}
            if node.get("type") == "object" and "properties" in node:
                node["required"] = list(node["properties"])
                node["additionalProperties"] = False
            return node
        if isinstance(node, list):
            return [walk(v) for v in node]
        return node

    return walk(schema.model_json_schema())


def _groq():
    try:
        import groq
    except ImportError as e:   # a core dependency; only missing in a broken install
        raise LlmError("the groq package is not installed: pip install -e .",
                       kind="missing_dependency") from e
    return groq


def _retry_after(exc: Any) -> float | None:
    response = getattr(exc, "response", None)
    value = response.headers.get("retry-after") if response is not None else None
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None


class GroqClient:
    """Structured calls (and, for the publishing agent, tool calls) to the free Groq API.

    - Strict JSON Schema output (`response_format.json_schema.strict`), validated again with
      pydantic after the `finish_reason` check.
    - The SDK's own retries are off. A 429 is waited out here, using `retry-after` when Groq
      sends it (else 2, 4, 8 … seconds), at most `rate_limit_retries` times. A wait longer
      than `max_wait_seconds` (such as the daily limit) fails at once with kind `rate_limit`.
    - Every call is logged with its token use and the rate-limit headers, so the logs show
      how close a run comes to the free tier's tokens-per-minute limit.
    - The SDK reads the key from GROQ_API_KEY; it is never logged.
    """

    def __init__(self, cfg: LlmConfig, *, log_dir: Path | None = None, client: Any | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.cfg: GroqConfig = cfg.groq
        self.log_dir = log_dir
        self.usage = Usage(provider="groq")
        self._sleep = sleep
        if client is None:
            if not os.environ.get(GROQ_KEY_ENV):
                raise LlmError(f"{GROQ_KEY_ENV} is not set: add it to .env (free key at "
                               "console.groq.com/keys)", kind="auth")
            client = _groq().Groq(max_retries=0, timeout=self.cfg.timeout_seconds)
        self._client = client
        self._lock = threading.Lock()
        self._seq = itertools.count(1)

    def complete(self, *, name: str, messages: list[dict[str, Any]], effort: str,
                 max_tokens: int | None = None, **extra: Any) -> Any:
        """One chat completion with 429 handling and logging. Returns the SDK's completion;
        the caller checks `finish_reason` and reads the message."""
        groq = _groq()
        kwargs: dict[str, Any] = {
            "model": self.cfg.model,
            "messages": messages,
            "reasoning_effort": _groq_effort(effort),
            "max_completion_tokens": max_tokens or self.cfg.max_completion_tokens,
            "temperature": 0.2,
            **extra,
        }
        with self._lock:
            seq = next(self._seq)
        waits: list[float] = []
        started = time.monotonic()
        while True:
            try:
                raw = self._client.chat.completions.with_raw_response.create(**kwargs)
                completion = raw.parse()
                headers = {h: raw.headers.get(h) for h in _RATE_HEADERS
                           if raw.headers.get(h) is not None}
                break
            except groq.RateLimitError as e:
                wait = _retry_after(e)
                wait = wait if wait is not None else float(2 ** (len(waits) + 1))
                if len(waits) >= self.cfg.rate_limit_retries or wait > self.cfg.max_wait_seconds:
                    self._log(seq, name, kwargs, None, None, {}, waits, error="rate_limit")
                    raise LlmError(f"{name}: rate limited by Groq (429) after {len(waits)} "
                                   f"waits; the next wait would be {wait:g}s",
                                   kind="rate_limit", retryable=True) from e
                waits.append(wait)
                with self._lock:
                    self.usage.rate_limit_waits += 1
                self._sleep(wait)
            except groq.AuthenticationError as e:
                raise LlmError(f"{name}: Groq rejected the API key ({GROQ_KEY_ENV})",
                               kind="auth") from e
            except groq.BadRequestError as e:
                # JSON output that misses the schema is reported as a 400.
                kind = "schema" if "schema" in str(e).lower() else "api_status"
                raise LlmError(f"{name}: Groq refused the request ({e.status_code}): "
                               f"{_short(e)}", kind=kind) from e
            except groq.APIStatusError as e:
                raise LlmError(f"{name}: Groq API error {e.status_code}: {_short(e)}",
                               kind="api_status", retryable=e.status_code >= 500) from e
            except groq.APIConnectionError as e:   # includes APITimeoutError
                raise LlmError(f"{name}: could not reach Groq: {e}", kind="connection",
                               retryable=True) from e
        call_usage = _groq_usage(completion, time.monotonic() - started)
        with self._lock:
            self.usage.add(call_usage)
        self._log(seq, name, kwargs, completion, call_usage, headers, waits)
        return completion

    def parse(self, *, name: str, system: str, prefix: str | None, user: str,
              schema: type[T], effort: Effort, max_tokens: int | None = None) -> T:
        content = f"{prefix}\n\n{user}" if prefix else user
        messages = [{"role": "system", "content": system}, {"role": "user", "content": content}]
        response_format = {"type": "json_schema", "json_schema": {
            "name": schema.__name__, "strict": True, "schema": strict_schema(schema)}}
        completion = self.complete(name=name, messages=messages, effort=effort,
                                   max_tokens=max_tokens, response_format=response_format)
        choice = completion.choices[0]
        if getattr(choice.message, "refusal", None):
            raise LlmRefusal(f"{name}: the model declined the request", category=None)
        if choice.finish_reason == "length":
            raise LlmError(f"{name}: the response hit max_completion_tokens and is incomplete",
                           kind="max_tokens")
        if choice.finish_reason != "stop":
            raise LlmError(f"{name}: unexpected finish_reason {choice.finish_reason!r}",
                           kind="schema")
        try:
            return schema.model_validate_json(choice.message.content or "")
        except ValidationError as e:
            raise LlmError(f"{name}: the response did not match the schema: "
                           f"{e.error_count()} errors", kind="schema") from e

    def _log(self, seq: int, name: str, kwargs: dict[str, Any], completion: Any,
             usage: Usage | None, headers: dict[str, str], waits: list[float], *,
             error: str | None = None) -> None:
        if self.log_dir is None:
            return
        request = {k: v for k, v in kwargs.items() if k != "response_format"}
        if "response_format" in kwargs:
            request["response_format"] = kwargs["response_format"]["json_schema"]["name"]
        request["provider"] = "groq"
        response: dict[str, Any] = {"rate_limit_waits_seconds": waits}
        if error:
            response["error"] = error
        if completion is not None:
            choice = completion.choices[0]
            text = choice.message.content
            try:
                output: Any = json.loads(text) if text else None
            except json.JSONDecodeError:
                output = text
            u = getattr(completion, "usage", None)
            details = getattr(u, "completion_tokens_details", None)
            response.update({
                "id": getattr(completion, "id", None),
                "model": getattr(completion, "model", None),
                "finish_reason": choice.finish_reason,
                "tokens": {
                    "prompt": getattr(u, "prompt_tokens", 0),
                    "completion": getattr(u, "completion_tokens", 0),
                    "reasoning": getattr(details, "reasoning_tokens", None),
                    "total": getattr(u, "total_tokens", 0),
                },
                # Groq's own count of what is left this minute / day (tokens = per minute).
                "rate_limit": headers,
                "seconds": round(usage.seconds, 2) if usage else None,
                "output": output,
                "tool_calls": [{"name": c.function.name, "arguments": c.function.arguments}
                               for c in (getattr(choice.message, "tool_calls", None) or [])],
            })
        write_json(self.log_dir / f"{seq:02d}_{name}.json",
                   {"request": request, "response": response})


def _groq_effort(effort: str) -> str:
    """Claude-style effort names mapped onto Groq's reasoning_effort (low, medium, high)."""
    return {"low": "low", "medium": "medium"}.get(effort, "high")


def _short(e: Any) -> str:
    return str(getattr(e, "message", e))[:300]


def _groq_usage(completion: Any, seconds: float) -> Usage:
    u = getattr(completion, "usage", None)
    return Usage(calls=1, input_tokens=getattr(u, "prompt_tokens", 0) or 0,
                 output_tokens=getattr(u, "completion_tokens", 0) or 0, seconds=seconds,
                 models={getattr(completion, "model", None) or "unknown": 1}, provider="groq")


def build_llm(cfg: LlmConfig, *, log_dir: Path | None = None) -> StructuredLlm:
    """The real client for `llm.provider`. Credentials come from the environment
    (GROQ_API_KEY; or ANTHROPIC_API_KEY / an `ant auth login` profile). The CLI calls this
    through the module so tests can patch it."""
    if cfg.provider == "groq":
        return GroqClient(cfg, log_dir=log_dir)
    return LlmClient(cfg, log_dir=log_dir)
