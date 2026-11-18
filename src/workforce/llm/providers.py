"""Direct, dependency-free provider probes: key validation, model listing, benchmarks.

API keys are sent only in request headers (never in URLs), and they never
appear in error messages.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass

GEMINI_API = "https://generativelanguage.googleapis.com/v1beta"
BENCH_PROMPT = "Write a short Python function that checks whether a number is prime."


class ProviderError(Exception):
    """A provider probe failed. The message is safe to show (no secrets)."""


def _request(url: str, *, headers: dict | None = None, body: dict | None = None,
             timeout: float = 15) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    # Some providers (e.g. Groq behind Cloudflare) reject urllib's default
    # User-Agent with a bare 403, so send a generic client name.
    req = urllib.request.Request(url, data=data, headers={
        "Content-Type": "application/json", "User-Agent": "ai-workforce/0.1",
        **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as exc:
        try:
            err = json.loads(exc.read() or b"{}").get("error", {})
        except ValueError:
            err = {}
        if isinstance(err, str):  # Ollama returns {"error": "..."}
            raise ProviderError(f"HTTP {exc.code}: {err[:200]}") from None
        status = err.get("status", "")
        msg = (err.get("message") or "").split("\n")[0][:200]
        raise ProviderError(f"HTTP {exc.code} {status}: {msg}".strip()) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ProviderError(f"unreachable: {reason}") from None


# ---------- Gemini ----------

def gemini_list_models(api_key: str) -> list[str]:
    """Model ids usable for text generation with this key (validates the key)."""
    models: list[str] = []
    token = ""
    while True:
        url = f"{GEMINI_API}/models?pageSize=200" + (f"&pageToken={token}" if token else "")
        data = _request(url, headers={"x-goog-api-key": api_key})
        for m in data.get("models", []):
            if "generateContent" in m.get("supportedGenerationMethods", []):
                models.append(m["name"].removeprefix("models/"))
        token = data.get("nextPageToken", "")
        if not token:
            return sorted(models)


@dataclass(frozen=True)
class ProbeResult:
    model: str
    ok: bool
    seconds: float
    detail: str


def gemini_probe(api_key: str, model: str, *, timeout: float = 60) -> ProbeResult:
    """Send one tiny prompt: is this model actually usable on this key/tier?"""
    import time

    started = time.monotonic()
    try:
        data = _request(
            f"{GEMINI_API}/models/{model}:generateContent",
            headers={"x-goog-api-key": api_key},
            body={"contents": [{"parts": [{"text": "Reply with exactly: PONG"}]}]},
            timeout=timeout,
        )
    except ProviderError as exc:
        return ProbeResult(model, False, round(time.monotonic() - started, 2), str(exc))
    parts = (data.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
    text = "".join(p.get("text", "") for p in parts).strip()
    return ProbeResult(model, True, round(time.monotonic() - started, 2), text[:40])


# ---------- OpenAI-compatible (Groq, OpenRouter, Ollama /v1, ...) ----------

def openai_list_models(base_url: str, api_key: str) -> list[str]:
    data = _request(f"{base_url}/models", headers={"Authorization": f"Bearer {api_key}"})
    return sorted(m["id"] for m in data.get("data", []))


def openai_probe(base_url: str, api_key: str, model: str, *, timeout: float = 120
                 ) -> ProbeResult:
    import time

    started = time.monotonic()
    try:
        data = _request(
            f"{base_url}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            body={"model": model, "max_tokens": 32,
                  "messages": [{"role": "user", "content": "Reply with exactly: PONG"}]},
            timeout=timeout,
        )
    except ProviderError as exc:
        return ProbeResult(model, False, round(time.monotonic() - started, 2), str(exc))
    text = ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
    return ProbeResult(model, True, round(time.monotonic() - started, 2), text.strip()[:40])


# ---------- provider-agnostic dispatch ----------

def api_base(provider) -> str:
    """OpenAI-compatible base URL for a configured provider."""
    base = provider.resolved_base_url()
    if provider.type == "ollama":
        return f"{base or 'http://localhost:11434'}/v1"
    if not base:
        raise ProviderError(f"provider type '{provider.type}' needs base_url in models.yaml")
    return base


def list_models(provider, api_key: str | None) -> list[str]:
    if provider.type == "gemini":
        return gemini_list_models(api_key or "")
    if provider.type == "ollama":
        return ollama_list_models(provider.resolved_base_url() or "http://localhost:11434")
    return openai_list_models(api_base(provider), api_key or "")


def probe(provider, api_key: str | None, model: str) -> ProbeResult:
    if provider.type == "gemini":
        return gemini_probe(api_key or "", model)
    return openai_probe(api_base(provider), api_key or "ollama", model,
                        timeout=provider.timeout_seconds)


# ---------- Ollama ----------

def ollama_version(base_url: str) -> str:
    return _request(f"{base_url}/api/version", timeout=3).get("version", "?")


def ollama_list_models(base_url: str) -> list[str]:
    data = _request(f"{base_url}/api/tags", timeout=5)
    return sorted(m["name"] for m in data.get("models", []))


@dataclass(frozen=True)
class BenchResult:
    model: str
    load_seconds: float
    prompt_tokens_per_second: float
    gen_tokens_per_second: float
    gen_tokens: int
    total_seconds: float


def ollama_benchmark(base_url: str, model: str, *, max_tokens: int = 128,
                     timeout: float = 600) -> BenchResult:
    data = _request(f"{base_url}/api/generate", timeout=timeout, body={
        "model": model,
        "prompt": BENCH_PROMPT,
        "stream": False,
        "keep_alive": "1m",  # free RAM soon after benchmarking
        "options": {"num_predict": max_tokens, "temperature": 0},
    })
    ns = 1e9

    def rate(count: int, dur: int) -> float:
        return round(count / (dur / ns), 2) if dur else 0.0

    return BenchResult(
        model=model,
        load_seconds=round(data.get("load_duration", 0) / ns, 2),
        prompt_tokens_per_second=rate(data.get("prompt_eval_count", 0),
                                      data.get("prompt_eval_duration", 0)),
        gen_tokens_per_second=rate(data.get("eval_count", 0), data.get("eval_duration", 0)),
        gen_tokens=data.get("eval_count", 0),
        total_seconds=round(data.get("total_duration", 0) / ns, 2),
    )
