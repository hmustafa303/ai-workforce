"""Map engine names from config/engines.yaml to adapter classes."""

from __future__ import annotations

from workforce.engines.base import Engine, EngineUnavailable
from workforce.engines.codex import CodexEngine
from workforce.engines.cursor import CursorEngine
from workforce.settings import Settings

ADAPTERS: dict[str, type[Engine]] = {"codex": CodexEngine, "cursor": CursorEngine}


def get_engine(settings: Settings, name: str, tier: str | None = None) -> Engine:
    spec = settings.engines.engines.get(name)
    if spec is None:
        known = ", ".join(settings.engines.engines) or "none"
        raise EngineUnavailable(f"unknown engine '{name}' (configured: {known})")
    adapter = ADAPTERS.get(spec.type)
    if adapter is None:
        raise EngineUnavailable(f"no adapter for engine type '{spec.type}' yet")
    return adapter(name, spec, tier)


def provider_secret_names(settings: Settings) -> set[str]:
    """Env var names of every provider key, to strip from child tools."""
    return {p.api_key_env for p in settings.models.providers.values() if p.api_key_env}
