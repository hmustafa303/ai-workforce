"""Load and validate workforce configuration (.env + config/*.yaml).

Secrets are never stored on the Settings object: only the *names* of the
environment variables. Use `secret_present()` to check whether one is set.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_FILES = ("models", "agents", "policies")
KNOWN_TOOLS = frozenset({"web_search", "fs_read", "worktree_write"})
WRITE_TOOLS = frozenset({"worktree_write"})


class ConfigError(Exception):
    """Configuration is missing or invalid. `errors` holds readable messages."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("; ".join(errors))


class _Strict(BaseModel):
    # extra="forbid" turns config typos (e.g. `alow_write`) into errors.
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------- models.yaml ----------

class Provider(_Strict):
    type: Literal["gemini", "ollama", "groq", "openrouter", "openai_compatible"]
    enabled: bool = True
    local: bool = False
    api_key_env: str | None = None
    base_url: str | None = None
    base_url_env: str | None = None
    rpm_limit: int | None = Field(default=None, gt=0)
    timeout_seconds: int = Field(default=120, gt=0)
    trains_on_prompts: bool = False

    def resolved_base_url(self) -> str | None:
        if self.base_url_env and os.environ.get(self.base_url_env, "").strip():
            return os.environ[self.base_url_env].strip().rstrip("/")
        return self.base_url.rstrip("/") if self.base_url else None


class ModelSpec(_Strict):
    provider: str
    model: str | None = None  # None = not chosen yet
    temperature: float = Field(default=0.2, ge=0, le=2)
    max_tokens: int | None = Field(default=None, gt=0)


class ModelsConfig(_Strict):
    providers: dict[str, Provider]
    models: dict[str, ModelSpec]
    chains: dict[str, list[str]]

    @model_validator(mode="after")
    def _check_refs(self) -> ModelsConfig:
        errors = []
        for name, spec in self.models.items():
            if spec.provider not in self.providers:
                errors.append(f"model '{name}' uses unknown provider '{spec.provider}'")
        for name, chain in self.chains.items():
            if not chain:
                errors.append(f"chain '{name}' is empty")
            for model_name in chain:
                if model_name not in self.models:
                    errors.append(f"chain '{name}' uses unknown model '{model_name}'")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    def chain_has_local(self, chain: str) -> bool:
        return any(self.providers[self.models[m].provider].local for m in self.chains[chain])


# ---------- agents.yaml ----------

class AgentSpec(_Strict):
    role: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    backstory: str = Field(min_length=1)
    chain: str
    tools: list[str] = Field(default_factory=list)
    allow_write: bool = False
    # Who does the work: "crew" = a CrewAI agent on the chain above, or the name
    # of a CLI engine from engines.yaml (e.g. "codex", "cursor").
    engine: str = "crew"
    max_iter: int = Field(default=15, ge=1, le=60)  # CrewAI tool-use steps

    @model_validator(mode="after")
    def _check_tools(self) -> AgentSpec:
        unknown = set(self.tools) - KNOWN_TOOLS
        if unknown:
            raise ValueError(f"unknown tools {sorted(unknown)}; known: {sorted(KNOWN_TOOLS)}")
        if WRITE_TOOLS & set(self.tools) and not self.allow_write:
            raise ValueError("write tools require allow_write: true")
        return self


class AgentsConfig(_Strict):
    agents: dict[str, AgentSpec] = Field(min_length=1)


# ---------- policies.yaml ----------

class TaskPolicy(_Strict):
    max_revisions: int = Field(default=3, ge=0, le=10)
    timeout_seconds: int = Field(default=1800, gt=0)
    max_concurrent: int = Field(default=1, ge=1)
    verify_timeout_seconds: int = Field(default=900, gt=0)  # per verification command


class PathPolicy(_Strict):
    workspace_dir: Path = Path("workspace")
    runs_dir: Path = Path("runs")
    ledger_db: Path = Path("runs/ledger.sqlite3")
    allowed_project_roots: list[Path] = Field(default_factory=list)
    denied_globs: list[str] = Field(default_factory=list)


class PrivacyPolicy(_Strict):
    default_local_only: bool = False
    local_only_projects: list[Path] = Field(default_factory=list)


class MergePolicy(_Strict):
    require_user_approval: bool = True


class PoliciesConfig(_Strict):
    tasks: TaskPolicy = TaskPolicy()
    paths: PathPolicy = PathPolicy()
    privacy: PrivacyPolicy = PrivacyPolicy()
    merge: MergePolicy = MergePolicy()


# ---------- engines.yaml (optional) ----------

class EngineSpec(_Strict):
    type: Literal["codex", "cursor"]
    enabled: bool = True
    binary: str | None = None  # None = auto-detect
    model: str | None = None
    timeout_seconds: int = Field(default=1200, gt=0)


class EnginesConfig(_Strict):
    engines: dict[str, EngineSpec] = Field(default_factory=dict)


# ---------- combined ----------

class Settings(_Strict):
    root: Path
    models: ModelsConfig
    agents: AgentsConfig
    policies: PoliciesConfig
    engines: EnginesConfig = EnginesConfig()

    @model_validator(mode="after")
    def _check_agent_chains(self) -> Settings:
        bad = [
            f"agent '{name}' uses unknown chain '{spec.chain}'"
            for name, spec in self.agents.agents.items()
            if spec.chain not in self.models.chains
        ]
        bad += [
            f"agent '{name}' uses unknown engine '{spec.engine}'"
            for name, spec in self.agents.agents.items()
            if spec.engine != "crew" and spec.engine not in self.engines.engines
        ]
        if bad:
            raise ValueError("; ".join(bad))
        return self

    @classmethod
    def load(cls, root: Path | None = None, *, load_env: bool = True) -> Settings:
        root = (root or PROJECT_ROOT).resolve()
        if load_env:
            # Real environment variables win over .env values.
            load_dotenv(root / ".env", override=False)
        raw = {name: _read_yaml(root / "config" / f"{name}.yaml") for name in CONFIG_FILES}
        engines_file = root / "config" / "engines.yaml"
        if engines_file.is_file():
            raw["engines"] = _read_yaml(engines_file)
        try:
            return cls(root=root, **raw)
        except ValidationError as exc:
            raise ConfigError(_format_validation(exc)) from None

    def resolve(self, path: Path) -> Path:
        """Resolve a config path relative to the project root."""
        return path if path.is_absolute() else (self.root / path).resolve()


def secret_present(env_name: str) -> bool:
    return bool(os.environ.get(env_name, "").strip())


def mask_secret(value: str | None) -> str:
    """Safe display form of a secret: only the last 4 chars of long values."""
    if not value:
        return "<empty>"
    return f"****{value[-4:]}" if len(value) > 12 else "****"


def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise ConfigError([f"missing config file: {path}"])
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError([f"{path.name}: invalid YAML: {exc}"]) from None
    if not isinstance(data, dict):
        raise ConfigError([f"{path.name}: top level must be a mapping"])
    return data


def _format_validation(exc: ValidationError) -> list[str]:
    out = []
    for err in exc.errors():
        loc = ".".join(str(p) for p in err["loc"])
        msg = err["msg"].removeprefix("Value error, ")
        out.append(f"{loc}: {msg}" if loc else msg)
    return out
