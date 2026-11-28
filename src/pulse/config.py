"""Load and validate config/pulse.yaml (architecture §8)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "pulse.yaml"

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)\}")
_EMAIL = re.compile(r"^[^@\s,;]+@[^@\s,;]+\.[A-Za-z]{2,}$")


class ConfigError(ValueError):
    """The config file is missing, unreadable, or invalid."""


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ProductConfig(_Strict):
    name: str
    play_package: str
    app_store_id: str = Field(pattern=r"^\d+$")
    app_store_country: str = Field(pattern=r"^[a-z]{2}$")


class WindowConfig(_Strict):
    weeks_back: int = Field(ge=8, le=12)
    timezone: str = "Asia/Kolkata"


class PathsConfig(_Strict):
    data_dir: str = "data"
    runs_dir: str = "runs"

    @property
    def data_path(self) -> Path:
        return _resolve(self.data_dir)

    @property
    def runs_path(self) -> Path:
        return _resolve(self.runs_dir)


def _resolve(p: str) -> Path:
    path = Path(p)
    return path if path.is_absolute() else PROJECT_ROOT / path


PLAY_FIELDS_REQUIRED = frozenset({"rating", "text", "date"})
PLAY_FIELDS_ALLOWED = PLAY_FIELDS_REQUIRED | {"review_id", "title", "updated", "app_version"}


class PlayPublicConfig(_Strict):
    enabled: bool = False
    langs: list[str] = Field(default_factory=lambda: ["en"], min_length=1)
    country: str = Field(default="in", pattern=r"^[a-z]{2}$")
    batch_size: int = Field(default=200, ge=10, le=500)
    max_reviews: int = Field(default=40_000, ge=100)


class IngestConfig(_Strict):
    request_delay_seconds: float = Field(default=1.5, ge=0)
    max_retries: int = Field(default=3, ge=0, le=10)
    user_agent: str
    app_store_max_pages: int = Field(default=10, ge=1, le=10)
    overlap_days: int = Field(default=3, ge=0, le=14)
    min_words: int = Field(default=9, ge=1)
    play_public: PlayPublicConfig = PlayPublicConfig()
    max_malformed_ratio: float = Field(default=0.10, ge=0, le=1)
    play_columns: dict[str, str]

    @field_validator("play_columns")
    @classmethod
    def _known_columns(cls, v: dict[str, str]) -> dict[str, str]:
        missing = PLAY_FIELDS_REQUIRED - v.keys()
        if missing:
            raise ValueError(f"play_columns is missing mappings for: {sorted(missing)}")
        unknown = v.keys() - PLAY_FIELDS_ALLOWED
        if unknown:
            raise ValueError(
                f"play_columns has unsupported fields {sorted(unknown)}; allowed: "
                f"{sorted(PLAY_FIELDS_ALLOWED)} (identity fields are never read)"
            )
        return v


class PrivacyConfig(_Strict):
    number_min_digits: int = Field(default=8, ge=6)
    reference_min_digits: int = Field(default=5, ge=3)


class LocalEngineConfig(_Strict):
    """The default engine (architecture §4.4): embeddings + k-means, rules and templates."""

    model: str = "minishlab/potion-base-8M"
    model_revision: str = Field(default="bf8b056651a2c21b8d2565580b8569da283cab23",
                                min_length=7)
    micro_clusters: int = Field(default=12, ge=4, le=40)
    micro_clusters_retry: int = Field(default=16, ge=4, le=60)  # when `other` is too large
    n_init: int = Field(default=50, ge=1, le=500)
    seed: int = 42
    min_stability: float = Field(default=0.75, ge=0, le=1)  # median ARI vs 3 other seeds
    other_cosine: float = Field(default=0.40, ge=0, le=1)
    secondary_margin: float = Field(default=0.05, ge=0, le=1)
    loose_cohesion: float = Field(default=0.55, ge=0, le=1)  # median cosine; below = `other`
    praise_words: list[str] = Field(min_length=1)
    specific_words: list[str] = Field(min_length=1)
    vague_words: list[str] = Field(min_length=1)
    name_allowlist: list[str] = Field(default_factory=list)
    # Subjects no theme covers (spam calls, privacy, ...): a review that mentions one is
    # never quoted, whichever theme it was clustered into.
    quote_off_topic_words: list[str] = Field(default_factory=list)


Engine = Literal["local", "llm"]


class AnalysisConfig(_Strict):
    engine: Engine = "local"
    theme_catalogue: str = "config/themes.yaml"
    local: LocalEngineConfig | None = None
    max_themes: int = Field(default=5, ge=3, le=5)
    top_themes: int = 3
    quotes: int = 3
    actions: int = 3
    max_words: int = Field(default=250, gt=0)
    min_reviews: int = Field(default=50, ge=1)
    quote_min_words: int = Field(default=8, ge=1)
    quote_max_words: int = Field(default=60, ge=1)
    seed_themes: list[str] = Field(default_factory=list)
    # Theme clustering (architecture §4.4, plan Phase 3 S1–S6).
    max_discovery_reviews: int = Field(default=3000, ge=100)  # above this, Pass A samples
    assign_batch_size: int = Field(default=500, ge=20, le=1000)
    assign_concurrency: int = Field(default=3, ge=1, le=8)
    min_theme_share: float = Field(default=0.03, ge=0, le=0.5)
    max_other_share: float = Field(default=0.25, ge=0, le=1)
    recency_days: int = Field(default=14, ge=1, le=56)
    consistency_sample: int = Field(default=100, ge=0)  # 0 turns the probe off
    min_agreement: float = Field(default=0.85, ge=0, le=1)

    @model_validator(mode="after")
    def _consistent(self) -> AnalysisConfig:
        if self.top_themes > self.max_themes:
            raise ValueError("top_themes cannot exceed max_themes")
        if self.quote_min_words > self.quote_max_words:
            raise ValueError("quote_min_words cannot exceed quote_max_words")
        if self.engine == "local" and self.local is None:
            raise ValueError("analysis.local is required when analysis.engine is local")
        return self

    @property
    def catalogue_path(self) -> Path:
        return _resolve(self.theme_catalogue)


Effort = Literal["low", "medium", "high", "xhigh", "max"]


class EffortConfig(_Strict):
    cluster: Effort = "high"
    assign: Effort = "medium"
    compose: Effort = "high"


Provider = Literal["groq", "anthropic"]
GroqEffort = Literal["low", "medium", "high"]


class GroqEffortConfig(_Strict):
    """Groq `reasoning_effort` per stage. Lower effort uses fewer reasoning tokens, which
    matters on the free tier's per-minute token limit."""

    compose: GroqEffort = "medium"
    agent: GroqEffort = "low"


class GroqConfig(_Strict):
    """The free Groq API (`GROQ_API_KEY`). `openai/gpt-oss-120b` supports strict JSON Schema
    output and tool calling (Groq docs, checked 2026-11-28)."""

    model: str = "openai/gpt-oss-120b"
    reasoning_effort: GroqEffortConfig = GroqEffortConfig()
    max_completion_tokens: int = Field(default=4096, ge=256, le=65_536)
    timeout_seconds: float = Field(default=60, gt=0)
    # On HTTP 429: wait for `retry-after` (or back off exponentially) and try again, at most
    # this many times. A wait longer than max_wait_seconds (e.g. the daily limit) fails at once.
    rate_limit_retries: int = Field(default=4, ge=0, le=10)
    max_wait_seconds: float = Field(default=90, gt=0)


class AnthropicConfig(_Strict):
    """The paid Claude API (`ANTHROPIC_API_KEY`, optional `[llm]` extra)."""

    model: str = "claude-opus-5-5"
    effort: EffortConfig = EffortConfig()
    fallbacks: str | None = "default"


class LlmConfig(_Strict):
    """Which model provider the LLM steps use. `provider` picks the block below."""

    provider: Provider = "groq"
    groq: GroqConfig = GroqConfig()
    anthropic: AnthropicConfig = AnthropicConfig()

    @model_validator(mode="before")
    @classmethod
    def _legacy_fields(cls, data: object) -> object:
        """Older configs set `model`, `effort` and `fallbacks` directly under `llm:`; those
        were the Claude settings, so they move into `anthropic:` (and imply that provider)."""
        if not isinstance(data, dict) or not ({"model", "effort", "fallbacks"} & data.keys()):
            return data
        data = dict(data)
        legacy = {k: data.pop(k) for k in ("model", "effort", "fallbacks") if k in data}
        data["anthropic"] = {**legacy, **(data.get("anthropic") or {})}
        data.setdefault("provider", "anthropic")
        return data

    @property
    def model(self) -> str:
        """The model of the active provider."""
        return self.groq.model if self.provider == "groq" else self.anthropic.model

    @property
    def effort(self) -> EffortConfig:
        """Claude effort levels (used by the Anthropic client and `--engine llm`)."""
        return self.anthropic.effort

    @property
    def fallbacks(self) -> str | None:
        return self.anthropic.fallbacks

    def stage_effort(self, stage: Literal["compose", "agent"]) -> Effort:
        """The effort level to pass for a stage, for whichever provider is active."""
        if self.provider == "groq":
            return getattr(self.groq.reasoning_effort, stage)
        return self.anthropic.effort.compose


_DOC_URL = re.compile(r"/document/(?:u/\d+/)?d/([A-Za-z0-9_-]+)")
_DOC_ID = re.compile(r"^[A-Za-z0-9_-]{20,}$")


class PublishConfig(_Strict):
    """Where the weekly pulse goes (architecture §4.7): one Google Doc, made by hand once and
    appended to every week, and one email recipient. The app never creates a doc."""

    doc_id: str | None = None
    recipient: str | None = None

    @field_validator("doc_id", "recipient", mode="before")
    @classmethod
    def _blank_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("doc_id")
    @classmethod
    def _doc_id(cls, v: str | None) -> str | None:
        """A Doc ID, or a docs.google.com URL (the ID is taken from it)."""
        if v is None:
            return v
        v = v.strip()
        m = _DOC_URL.search(v)
        v = m.group(1) if m else v
        if not _DOC_ID.match(v):
            raise ValueError("doc_id (PULSE_DOC_ID) must be a Google Doc ID or URL")
        return v

    @property
    def doc_url(self) -> str | None:
        return f"https://docs.google.com/document/d/{self.doc_id}/edit" if self.doc_id else None

    @field_validator("recipient")
    @classmethod
    def _one_valid_address(cls, v: str | None) -> str | None:
        # Edge case GML-01: exactly one valid address, never a list.
        if v is not None and not _EMAIL.match(v.strip()):
            raise ValueError("recipient must be exactly one valid email address")
        return v.strip() if v else v


class McpServerConfig(_Strict):
    """How to reach one MCP server (architecture §4.7): a local command (stdio) or a URL
    (Streamable HTTP). Header values usually come from .env (`${PULSE_MCP_API_KEY}`) and are
    kept as secrets, so they never appear in logs, reprs or run artifacts."""

    transport: Literal["stdio", "http"] = "stdio"
    command: str = ""
    args: list[str] = Field(default_factory=list)
    url: str | None = None
    headers: dict[str, SecretStr] = Field(default_factory=dict)
    # HTTP only: polled until it answers 200 before connecting (a free Render service sleeps
    # after 15 idle minutes and can take about a minute to wake).
    health_url: str | None = None
    wake_timeout_seconds: float = Field(default=120, gt=0)
    connect_timeout_seconds: float = Field(default=60, gt=0)
    call_timeout_seconds: float = Field(default=90, gt=0)   # MCP-07: never hang on a call

    @field_validator("url", "health_url", mode="before")
    @classmethod
    def _blank_url_is_none(cls, v: object) -> object:
        return None if isinstance(v, str) and not v.strip() else v

    @property
    def configured(self) -> bool:
        return bool(self.url) if self.transport == "http" else bool(self.command.strip())

    @property
    def missing_headers(self) -> list[str]:
        """Headers whose value is empty after ${VAR} expansion (e.g. 'Bearer ' with no key)."""
        out = []
        for name, value in self.headers.items():
            v = value.get_secret_value().strip()
            if not v or v.lower() in ("bearer", "basic"):
                out.append(name)
        return out


class McpConfig(_Strict):
    docs: McpServerConfig
    gmail: McpServerConfig
    tool_map: str
    # Publishing retries a lost connection or a timeout this many times, waiting
    # retry_delay_seconds and doubling it each time, wherever that cannot publish twice.
    retries: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=10, ge=0)

    @property
    def tool_map_path(self) -> Path:
        return _resolve(self.tool_map)


class PulseConfig(_Strict):
    product: ProductConfig
    window: WindowConfig
    paths: PathsConfig = PathsConfig()
    ingest: IngestConfig
    privacy: PrivacyConfig = PrivacyConfig()
    analysis: AnalysisConfig
    llm: LlmConfig
    publish: PublishConfig
    mcp: McpConfig

    @model_validator(mode="after")
    def _engine_provider(self) -> PulseConfig:
        check_engine_provider(self.analysis.engine, self.llm.provider)
        return self


def check_engine_provider(engine: str, provider: str) -> None:
    """`--engine llm` clusters every review with the model: far beyond Groq's free daily token
    limit, so it needs the Claude API."""
    if engine == "llm" and provider != "anthropic":
        raise ValueError("analysis.engine: llm clusters every review with the model and needs "
                         f"llm.provider: anthropic (it is {provider!r}; Groq's free tier is too "
                         "small for it). Use the default hybrid engine with Groq instead.")


def _expand_env(value: object) -> object:
    """Replace ${VAR} with the environment value (empty string when unset)."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: Path | str | None = None, *, load_env_file: bool = True) -> PulseConfig:
    """Read the YAML config, expand ${VAR} references, and validate it."""
    if load_env_file:
        load_dotenv(PROJECT_ROOT / ".env", override=False)
    cfg_path = Path(path) if path else DEFAULT_CONFIG_PATH
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    except FileNotFoundError as e:
        raise ConfigError(f"Config file not found: {cfg_path}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Config file is not valid YAML: {cfg_path}: {e}") from e
    if not isinstance(raw, dict):
        raise ConfigError(f"Config file must be a mapping: {cfg_path}")
    try:
        return PulseConfig.model_validate(_expand_env(raw))
    except ValueError as e:
        raise ConfigError(f"Invalid config in {cfg_path}:\n{e}") from e
