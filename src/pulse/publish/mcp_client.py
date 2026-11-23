"""MCP client layer (architecture §4.7): the tool map, connecting, and calling tools.

The app is an MCP client only. It never talks to Google itself: the configured MCP server owns
the OAuth tokens and the Google API calls. Publisher code calls *logical* actions
(`append_to_doc`, `doc_contains_text`, `send_email`), which the tool map (`tool_map.yaml`)
resolves to the server's tool names, arguments and result patterns.

Safety rules enforced here:
- MCP-03: a tool whose name contains "send" (any case) may only be mapped to the one
  `gmail.send_email` action, and that action takes exactly `to`, `subject` and `body` from the
  publisher (no cc/bcc, nothing fixed in the map). The publisher only ever passes
  PULSE_RECIPIENT as `to` (email_sender.py).
- MCP-04: an argument whose name could trigger sending (`send_now`, ...) is refused.
- MCP-10: `create_doc` can never be mapped: the pulse goes into one doc made by hand.
- MCP-01/02: every mapped tool must exist in `list_tools()` with a compatible input schema.
- MCP-07/06: connecting and every call have timeouts, so an unattended run never hangs.
- MCP-08: a result with `isError`, or whose text matches neither the `ok` nor the `not_found`
  pattern, is a failure.
- MCP-09: stdio servers get the command and arguments as a list, never a shell string.
Header values (the API key) are secrets: they are never logged or written to artifacts.
"""

from __future__ import annotations

import math
import re
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import anyio
import httpx
import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

from pulse.config import McpServerConfig

REQUIRED_ACTIONS = {
    # doc_contains_text is the "already published?" check; without it nothing is published.
    "docs": ("doc_contains_text", "append_to_doc"),
    "gmail": ("send_email",),
}
OPTIONAL_ACTIONS: dict[str, tuple[str, ...]] = {"docs": (), "gmail": ()}
SEND_ACTION = ("gmail", "send_email")
SEND_ARGS = {"to": "{to}", "subject": "{subject}", "body": "{body}"}
FORBIDDEN_TOOLS = {"create_doc"}                  # MCP-10
_PLACEHOLDER = re.compile(r"^\{(\w+)\}$")
_SEND = re.compile(r"send", re.I)


class McpSetupError(RuntimeError):
    """The tool map or the server cannot be used: nothing is called."""


class McpConnectError(McpSetupError):
    """The server could not be reached (not healthy in time, connection refused or timed out).
    Transient: worth retrying. A rejected API key is a plain McpSetupError."""


class McpCallError(RuntimeError):
    """A tool call failed (error result, unexpected text, timeout, lost connection)."""


class McpToolError(McpCallError):
    """The server answered, but its tool reported an error (`isError`, or text that is neither
    success nor not-found). Retrying will not help until the server or its setup is fixed."""

    def __init__(self, tool: str, message: str) -> None:
        super().__init__(message)
        self.tool = tool


def is_transient(e: BaseException) -> bool:
    """A failure that may pass on its own: no connection, a timeout, a lost connection. A tool
    error or a setup problem (tool map, missing or rejected key) is not."""
    if isinstance(e, McpToolError):
        return False
    return isinstance(e, (McpConnectError, McpCallError))


def mcp_error(exc: BaseException) -> McpSetupError | McpCallError | None:
    """The McpSetupError/McpCallError inside `exc`. MCP clients run in task groups, which wrap
    errors raised in their scope in (nested) exception groups."""
    if isinstance(exc, (McpSetupError, McpCallError)):
        return exc
    if isinstance(exc, BaseExceptionGroup):
        for inner in exc.exceptions:
            found = mcp_error(inner)
            if found is not None:
                return found
    return None


# ---------------------------------------------------------------------------- tool map


class ActionSpec(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    tool: str = Field(min_length=1)
    args: dict[str, Any] = Field(default_factory=dict)
    ok: str
    not_found: str | None = None

    @field_validator("tool")
    @classmethod
    def _never_create(cls, v: str) -> str:
        if v in FORBIDDEN_TOOLS:                              # MCP-10
            raise ValueError(f"tool {v!r} is never called: the pulse is appended to one "
                             "existing doc (PULSE_DOC_ID)")
        return v

    @field_validator("args")
    @classmethod
    def _no_send_args(cls, v: dict[str, Any]) -> dict[str, Any]:
        for key in v:                                          # MCP-04
            if _SEND.search(key):
                raise ValueError(f"argument {key!r} could trigger sending; it is not allowed")
        return v

    @field_validator("ok", "not_found")
    @classmethod
    def _regex(cls, v: str | None) -> str | None:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"bad pattern {v!r}: {e}") from e
        return v

    def arguments(self, values: Mapping[str, Any]) -> dict[str, Any]:
        """The tool arguments, with each "{name}" value replaced by `values[name]`."""
        out = {}
        for key, raw in self.args.items():
            m = _PLACEHOLDER.match(raw) if isinstance(raw, str) else None
            if m:
                if m.group(1) not in values:
                    raise McpSetupError(f"tool {self.tool}: no value for {raw}")
                out[key] = values[m.group(1)]
            else:
                out[key] = raw
        return out


class ToolMap(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    docs: dict[str, ActionSpec | None]
    gmail: dict[str, ActionSpec | None]

    def spec(self, server: Literal["docs", "gmail"], action: str) -> ActionSpec | None:
        return getattr(self, server).get(action)

    def mapped(self) -> list[tuple[str, str, ActionSpec]]:
        return [(srv, name, spec) for srv in ("docs", "gmail")
                for name, spec in getattr(self, srv).items() if spec is not None]


def load_tool_map(path: Path) -> ToolMap:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise McpSetupError(f"tool map not found: {path}") from e
    except yaml.YAMLError as e:
        raise McpSetupError(f"tool map is not valid YAML: {path}: {e}") from e
    try:
        tm = ToolMap.model_validate(raw)
    except ValueError as e:
        raise McpSetupError(f"invalid tool map {path}:\n{e}") from e
    for srv, action, spec in tm.mapped():                         # MCP-03
        if _SEND.search(spec.tool) and (srv, action) != SEND_ACTION:
            raise McpSetupError(f"tool map {path}: {srv}.{action} maps {spec.tool!r}, which "
                                "looks like it sends email; only gmail.send_email may send")
    send = tm.spec(*SEND_ACTION)
    if send is not None and send.args != SEND_ARGS:
        raise McpSetupError(f"tool map {path}: gmail.send_email must take exactly {SEND_ARGS} "
                            "(no cc, bcc or fixed values)")
    for srv, names in REQUIRED_ACTIONS.items():
        missing = [n for n in names if tm.spec(srv, n) is None]   # type: ignore[arg-type]
        if missing:
            raise McpSetupError(f"tool map {path}: {srv} needs {', '.join(missing)}")
    known = {**{s: set(REQUIRED_ACTIONS[s]) | set(OPTIONAL_ACTIONS[s]) for s in REQUIRED_ACTIONS}}
    for srv in ("docs", "gmail"):
        extra = set(getattr(tm, srv)) - known[srv]
        if extra:
            raise McpSetupError(f"tool map {path}: unknown {srv} actions {sorted(extra)}")
    return tm


def check_tools(tm: ToolMap, server: str, tools: list[Any]) -> list[str]:
    """Problems with the map against one server's `list_tools()` (MCP-01, MCP-02)."""
    by_name = {t.name: t for t in tools}
    problems = []
    for srv, action, spec in tm.mapped():
        if srv != server:
            continue
        tool = by_name.get(spec.tool)
        if tool is None:
            problems.append(f"{srv}.{action}: the server has no tool {spec.tool!r}")
            continue
        schema = tool.input_schema or {}
        props = set(schema.get("properties") or {})
        unknown = sorted(set(spec.args) - props)
        if unknown and not schema.get("additionalProperties", False):
            problems.append(f"{srv}.{action}: {spec.tool} takes no argument(s) {unknown}")
        missing = sorted(set(schema.get("required") or []) - set(spec.args))
        if missing:
            problems.append(f"{srv}.{action}: {spec.tool} requires {missing}")
    return problems


# ---------------------------------------------------------------------------- results


@dataclass
class ToolOutcome:
    status: Literal["ok", "not_found"]
    text: str
    groups: dict[str, str] = field(default_factory=dict)


def result_text(result: Any) -> str:
    return "\n".join(getattr(c, "text", "") for c in (result.content or [])).strip()


def interpret(spec: ActionSpec, result: Any) -> ToolOutcome:
    text = result_text(result)
    if result.is_error:
        raise McpToolError(spec.tool, f"{spec.tool} failed: {text or 'error result with no text'}")
    m = re.search(spec.ok, text, re.S)
    if m:
        return ToolOutcome("ok", text, {k: v for k, v in m.groupdict().items() if v is not None})
    if spec.not_found and re.search(spec.not_found, text, re.S):
        return ToolOutcome("not_found", text)
    raise McpToolError(spec.tool, f"{spec.tool} failed: {text[:300] or 'empty result'}")


# ---------------------------------------------------------------------------- connecting


def describe(server: McpServerConfig) -> str:
    """Where the server is, safe to log (no header values)."""
    if server.transport == "http":
        return server.url or "(no url)"
    return " ".join([server.command, *server.args]).strip() or "(no command)"


def wait_until_healthy(server: McpServerConfig, *, sleep=time.sleep, clock=time.monotonic,
                       client: httpx.Client | None = None) -> float:
    """Poll `health_url` until it answers 200 (a sleeping Render service takes up to a
    minute). Returns the seconds waited; raises McpSetupError after `wake_timeout_seconds`."""
    if server.transport != "http" or not server.health_url:
        return 0.0
    started = clock()
    deadline = started + server.wake_timeout_seconds
    http = client or httpx.Client(timeout=30)
    last = "no answer"
    try:
        while True:
            try:
                r = http.get(server.health_url)
                if r.status_code == 200:
                    return round(clock() - started, 1)
                last = f"HTTP {r.status_code}"
            except httpx.HTTPError as e:
                last = type(e).__name__
            if clock() >= deadline:
                raise McpConnectError(f"MCP server is not healthy at {server.health_url} after "
                                    f"{server.wake_timeout_seconds:.0f}s ({last})")
            sleep(5)
    finally:
        if client is None:
            http.close()


def _transport(server: McpServerConfig) -> Any:
    if server.transport == "http":
        import httpx2
        from mcp.client.streamable_http import streamable_http_client

        if not server.url:
            raise McpSetupError("mcp server has transport http but no url")
        if server.missing_headers:
            raise McpSetupError(
                f"MCP header(s) {', '.join(server.missing_headers)} are empty: set "
                "PULSE_MCP_API_KEY in .env")
        headers = {k: v.get_secret_value() for k, v in server.headers.items()}
        http = httpx2.AsyncClient(headers=headers, timeout=httpx2.Timeout(
            server.connect_timeout_seconds, read=server.call_timeout_seconds))
        return streamable_http_client(server.url, http_client=http)
    from mcp import StdioServerParameters

    if not server.command.strip():
        raise McpSetupError("mcp server has transport stdio but no command")
    return StdioServerParameters(command=server.command, args=list(server.args))  # MCP-09


def _connect_message(server: McpServerConfig, exc: BaseException) -> str:
    leaves = [exc]
    while leaves and isinstance(leaves[0], BaseExceptionGroup):
        leaves = list(leaves[0].exceptions)
    detail = "; ".join(f"{type(e).__name__}: {e}" for e in leaves)[:300]
    hint = ""
    if "401" in detail or "nauthorized" in detail:
        hint = " (the server rejected the API key: check PULSE_MCP_API_KEY)"
    return f"cannot connect to the MCP server at {describe(server)}: {detail}{hint}"


@asynccontextmanager
async def open_client(server: McpServerConfig) -> AsyncIterator[Any]:
    """An initialized MCP `Client` for one server. Tests replace this function with one that
    yields a client connected to an in-process fake server."""
    from mcp import Client

    client = Client(_transport(server), read_timeout_seconds=server.call_timeout_seconds)
    # The client runs a task group for its whole lifetime, so the connect timeout must be a
    # cancel scope around all of it (a scope around __aenter__ alone is exited while that task
    # group is still open, which anyio refuses). The deadline is lifted once connected.
    connected = False
    try:
        with anyio.CancelScope(
                deadline=anyio.current_time() + server.connect_timeout_seconds) as scope:
            async with client:
                connected = True
                scope.deadline = math.inf
                yield client
    except (McpSetupError, McpCallError):
        raise
    except BaseException as e:  # noqa: BLE001 — transports raise exception groups
        if connected or not isinstance(e, Exception):
            raise
        message = _connect_message(server, e)
        rejected = "rejected the API key" in message       # retrying will not fix the key
        raise (McpSetupError if rejected else McpConnectError)(message) from e
    if scope.cancelled_caught and not connected:
        raise McpConnectError(f"the MCP server at {describe(server)} did not answer within "
                            f"{server.connect_timeout_seconds:.0f}s")


class Caller:
    """Calls logical actions on one connected server."""

    def __init__(self, client: Any, tool_map: ToolMap, server: Literal["docs", "gmail"],
                 config: McpServerConfig) -> None:
        self.client, self.tool_map, self.server, self.config = client, tool_map, server, config
        self.calls: list[dict[str, Any]] = []      # action, tool, status, seconds (no payloads)
        self.listed: set[str] | None = None        # tool names from list_tools(), once checked

    def has(self, action: str) -> bool:
        """The action is mapped and, if the server's tools were listed, the server has it."""
        spec = self.tool_map.spec(self.server, action)
        return spec is not None and (self.listed is None or spec.tool in self.listed)

    async def call(self, action: str, **values: Any) -> ToolOutcome:
        spec = self.tool_map.spec(self.server, action)
        if spec is None:
            raise McpSetupError(f"{self.server}.{action} is not mapped")
        if not self.has(action):
            raise McpSetupError(f"the MCP server has no tool {spec.tool!r} for "
                                f"{self.server}.{action}")
        started = time.monotonic()
        status = "error"
        try:
            with anyio.fail_after(self.config.call_timeout_seconds):
                result = await self.client.call_tool(spec.tool, spec.arguments(values))
            outcome = interpret(spec, result)
            status = outcome.status
            return outcome
        except TimeoutError as e:
            raise McpCallError(f"{spec.tool} did not answer within "
                               f"{self.config.call_timeout_seconds:.0f}s") from e
        except (McpCallError, McpSetupError):
            raise
        except Exception as e:  # noqa: BLE001 — protocol or transport failure
            raise McpCallError(f"{spec.tool} failed: {type(e).__name__}: {e}"[:300]) from e
        finally:
            self.calls.append({"action": action, "tool": spec.tool, "status": status,
                               "seconds": round(time.monotonic() - started, 2)})


async def verified_caller(client: Any, tool_map: ToolMap, server: Literal["docs", "gmail"],
                          config: McpServerConfig) -> Caller:
    """A Caller, after checking the map against the server's tools (MCP-01/02)."""
    try:
        with anyio.fail_after(config.call_timeout_seconds):
            tools = (await client.list_tools()).tools
    except TimeoutError as e:
        raise McpCallError(f"list_tools did not answer within "
                           f"{config.call_timeout_seconds:.0f}s") from e
    problems = check_tools(tool_map, server, tools)
    if problems:
        raise McpSetupError("the MCP tools do not match the tool map: " + "; ".join(problems))
    caller = Caller(client, tool_map, server, config)
    caller.listed = {t.name for t in tools}
    return caller
