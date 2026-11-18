"""Turn a fallback chain from models.yaml into one CrewAI-compatible LLM.

`FallbackLLM` tries each model in order:
  * quota/rate-limit errors -> short backoff and retry, then the next model
    (if the provider asks for a long wait, skip straight to the next model)
  * auth / not-found / unreachable / other errors -> the next model immediately
  * every model failed -> ChainExhaustedError

Privacy: with `local_only=True`, only providers marked `local: true` are used.
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from crewai import LLM
from crewai.llms.base_llm import BaseLLM
from pydantic import PrivateAttr

from workforce.settings import ModelSpec, Provider, Settings, secret_present

# A one-off malformed tool call by the model (Groq: HTTP 400 tool_use_failed).
# Retrying the same model usually works and is far faster than a local fallback.
_BAD_GENERATION_MARKERS = ("tool_use_failed", "failed to call a function")
_QUOTA_MARKERS = ("429", "rate limit", "rate-limit", "resource_exhausted",
                  "resource exhausted", "too many requests", "quota")
_AUTH_MARKERS = ("401", "403", "api key", "api_key", "permission denied", "unauthorized")
_NOT_FOUND_MARKERS = ("404", "not found", "not_found")
_UNREACHABLE_MARKERS = ("connection", "connect error", "timed out", "timeout", "unreachable")
_RETRY_AFTER = re.compile(r"retry(?:[_ -]?delay|[_ -]?after)\W{0,4}(\d+(?:\.\d+)?)", re.I)


class NoUsableModelError(RuntimeError):
    """No model in the chain can be used (unset, disabled, missing key, privacy)."""


@dataclass(frozen=True)
class Failure:
    model: str
    kind: str
    detail: str


class ChainExhaustedError(RuntimeError):
    """Every model in the chain failed. Message deliberately avoids rate-limit
    wording so CrewAI's outer retry wrapper does not re-run the whole chain."""

    def __init__(self, chain: str, failures: list[Failure]):
        self.chain = chain
        self.failures = failures
        summary = "; ".join(f"{f.model}={f.kind}" for f in failures)
        super().__init__(f"all models in chain '{chain}' failed: {summary}")


@dataclass(frozen=True)
class Candidate:
    name: str          # logical name, e.g. "cloud_fast"
    provider_name: str
    provider: Provider
    spec: ModelSpec

    @property
    def label(self) -> str:
        return f"{self.name} ({self.provider_name}/{self.spec.model})"


def classify_error(exc: BaseException) -> str:
    status = getattr(exc, "status_code", None) or getattr(exc, "code", None)
    text = f"{type(exc).__name__} {status} {exc}".lower()
    if any(m in text for m in _BAD_GENERATION_MARKERS):
        return "bad_generation"
    if status == 429 or any(m in text for m in _QUOTA_MARKERS):
        return "quota"
    if status in (401, 403) or any(m in text for m in _AUTH_MARKERS):
        return "auth"
    if status == 404 or any(m in text for m in _NOT_FOUND_MARKERS):
        return "not_found"
    if isinstance(exc, (ConnectionError, TimeoutError)) or any(
            m in text for m in _UNREACHABLE_MARKERS):
        return "unreachable"
    return "error"


def retry_after_seconds(exc: BaseException) -> float | None:
    match = _RETRY_AFTER.search(str(exc))
    return float(match.group(1)) if match else None


def is_local_only(settings: Settings, project_path: Path | str | None = None) -> bool:
    privacy = settings.policies.privacy
    if privacy.default_local_only:
        return True
    if project_path is None:
        return False
    target = Path(project_path).resolve()
    return any(target.is_relative_to(Path(p).resolve()) for p in privacy.local_only_projects)


def candidates(settings: Settings, chain: str | None = None, *,
               models: list[str] | None = None, local_only: bool = False
               ) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """Usable models (of a chain, or an explicit list of logical models) in
    order, plus (model, reason) for each skipped one."""
    if models is None:
        if chain not in settings.models.chains:
            raise KeyError(f"unknown chain '{chain}'")
        models = settings.models.chains[chain]
    unknown = [m for m in models if m not in settings.models.models]
    if unknown:
        raise KeyError(f"unknown model(s): {', '.join(unknown)}")
    usable, skipped = [], []
    for name in models:
        spec = settings.models.models[name]
        provider = settings.models.providers[spec.provider]
        if spec.model is None:
            skipped.append((name, "model not chosen"))
        elif not provider.enabled:
            skipped.append((name, f"provider '{spec.provider}' disabled"))
        elif local_only and not provider.local:
            skipped.append((name, "cloud model blocked by local_only"))
        elif provider.api_key_env and not secret_present(provider.api_key_env):
            skipped.append((name, f"{provider.api_key_env} not set"))
        else:
            usable.append(Candidate(name, spec.provider, provider, spec))
    return usable, skipped


def make_llm(c: Candidate) -> BaseLLM:
    """Build the concrete CrewAI LLM for one candidate."""
    p, spec = c.provider, c.spec
    common: dict[str, Any] = {"temperature": spec.temperature}
    if spec.max_tokens:
        common["max_tokens"] = spec.max_tokens
    key = os.environ.get(p.api_key_env, "").strip() if p.api_key_env else None

    if p.type == "gemini":
        return LLM(model=f"gemini/{spec.model}", api_key=key, **common)
    if p.type == "ollama":
        base = p.resolved_base_url() or "http://localhost:11434"
        return LLM(model=f"ollama/{spec.model}", base_url=f"{base}/v1", api_key="ollama",
                   timeout=p.timeout_seconds, max_retries=0, **common)
    if p.type == "openrouter":
        return LLM(model=f"openrouter/{spec.model}", api_key=key,
                   timeout=p.timeout_seconds, max_retries=0, **common)
    if p.type in ("groq", "openai_compatible"):
        base = p.resolved_base_url() or (
            "https://api.groq.com/openai/v1" if p.type == "groq" else None)
        if not base:
            raise NoUsableModelError(f"{c.name}: openai_compatible provider needs base_url")
        return LLM(model=f"openai/{spec.model}", base_url=base, api_key=key,
                   timeout=p.timeout_seconds, max_retries=0, **common)
    raise NoUsableModelError(f"{c.name}: unsupported provider type '{p.type}'")


class FallbackLLM(BaseLLM):
    """A CrewAI LLM that delegates to an ordered list of member LLMs."""

    llm_type: str = "fallback"
    chain: str
    members: list[tuple[str, BaseLLM]]
    quota_retries: int = 2          # extra attempts per model on rate limits
    max_wait_seconds: float = 20.0  # longer provider-requested waits -> fall back

    _sleep: Callable[[float], None] = PrivateAttr(default=time.sleep)
    _history: list[dict] = PrivateAttr(default_factory=list)
    _last_used: str | None = PrivateAttr(default=None)

    @property
    def last_used(self) -> str | None:
        """Label of the model that produced the most recent answer."""
        return self._last_used

    @property
    def history(self) -> list[dict]:
        """Every attempt: model, outcome (ok/kind), seconds."""
        return list(self._history)

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        failures: list[Failure] = []
        for label, llm in self.members:
            llm.stop = list(self.stop)
            for attempt in range(self.quota_retries + 1):
                started = time.monotonic()
                try:
                    result = llm.call(
                        messages, tools=tools, callbacks=callbacks,
                        available_functions=available_functions, from_task=from_task,
                        from_agent=from_agent, response_model=response_model)
                except Exception as exc:  # noqa: BLE001 - classify any provider failure
                    kind = classify_error(exc)
                    self._record(label, kind, started)
                    failures.append(Failure(label, kind, str(exc)[:300]))
                    if kind == "bad_generation" and attempt < self.quota_retries:
                        continue  # same model, no wait
                    if kind == "quota" and attempt < self.quota_retries:
                        wait = retry_after_seconds(exc)
                        if wait is None:
                            wait = 2.0 * (2 ** attempt)
                        if wait <= self.max_wait_seconds:
                            self._sleep(wait)
                            continue
                    break  # next model
                else:
                    self._record(label, "ok", started)
                    self._last_used = label
                    return result
        # Raised outside the except block on purpose: no exception context is
        # chained, so CrewAI's rate-limit wrapper won't re-run the whole chain.
        raise ChainExhaustedError(self.chain, failures)

    async def acall(self, messages, tools=None, callbacks=None, available_functions=None,
                    from_task=None, from_agent=None, response_model=None):
        return await asyncio.to_thread(
            self.call, messages, tools, callbacks, available_functions,
            from_task, from_agent, response_model)

    def _record(self, label: str, outcome: str, started: float) -> None:
        self._history.append({"model": label, "outcome": outcome,
                              "seconds": round(time.monotonic() - started, 2)})

    def supports_function_calling(self) -> bool:
        return all(m.supports_function_calling() for _, m in self.members)

    def supports_stop_words(self) -> bool:
        return all(m.supports_stop_words() for _, m in self.members)

    def supports_multimodal(self) -> bool:
        return all(m.supports_multimodal() for _, m in self.members)

    def get_context_window_size(self) -> int:
        return min(m.get_context_window_size() for _, m in self.members)


def build_llm(settings: Settings, chain: str | None = None, *,
              models: list[str] | None = None, local_only: bool = False) -> FallbackLLM:
    """Build a FallbackLLM for a named chain, or for an explicit model list."""
    name = chain or "+".join(models or [])
    usable, skipped = candidates(settings, chain, models=models, local_only=local_only)
    if not usable:
        why = ", ".join(f"{n}: {r}" for n, r in skipped)
        raise NoUsableModelError(f"'{name}' has no usable model ({why})")
    return FallbackLLM(model=f"fallback:{name}", chain=name,
                       members=[(c.label, make_llm(c)) for c in usable])
