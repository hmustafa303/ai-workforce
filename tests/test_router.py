"""Router tests use fake member LLMs: no network, no keys, no Ollama needed."""

import asyncio
from typing import Any

import pytest
from conftest import edit_config, load_settings, set_models
from crewai.llms.base_llm import BaseLLM

from workforce.llm.router import (
    ChainExhaustedError,
    FallbackLLM,
    NoUsableModelError,
    build_llm,
    candidates,
    classify_error,
    is_local_only,
    make_llm,
    retry_after_seconds,
)


class HTTPError(Exception):
    def __init__(self, status: int, message: str = ""):
        super().__init__(message or f"HTTP {status}")
        self.status_code = status


class FakeLLM(BaseLLM):
    script: list[Any]   # each step: a return value, or an exception to raise
    calls: int = 0

    def call(self, messages, tools=None, callbacks=None, available_functions=None,
             from_task=None, from_agent=None, response_model=None):
        step = self.script[min(self.calls, len(self.script) - 1)]
        self.calls += 1
        if isinstance(step, BaseException):
            raise step
        return step


def fallback(*scripts):
    members = [(f"m{i}", FakeLLM(model=f"fake{i}", script=list(s))) for i, s in enumerate(scripts)]
    llm = FallbackLLM(model="fallback:test", chain="test", members=members)
    sleeps: list[float] = []
    llm._sleep = sleeps.append
    return llm, [m for _, m in members], sleeps


def test_first_model_answers():
    llm, (a, b), _ = fallback(["hello"], ["unused"])
    assert llm.call("hi") == "hello"
    assert llm.last_used == "m0"
    assert (a.calls, b.calls) == (1, 0)


def test_quota_error_retries_same_model_with_backoff():
    llm, (a, b), sleeps = fallback([HTTPError(429), "ok"], ["unused"])
    assert llm.call("hi") == "ok"
    assert llm.last_used == "m0"
    assert sleeps == [2.0]
    assert (a.calls, b.calls) == (2, 0)


def test_quota_exhausted_falls_back_to_next_model():
    llm, (a, b), sleeps = fallback([HTTPError(429)], ["local answer"])
    assert llm.call("hi") == "local answer"
    assert llm.last_used == "m1"
    assert a.calls == 3 and sleeps == [2.0, 4.0]
    assert [h["outcome"] for h in llm.history] == ["quota", "quota", "quota", "ok"]


def test_long_provider_retry_delay_falls_back_immediately():
    err = Exception("429 RESOURCE_EXHAUSTED. Please retry. 'retryDelay': '45s'")
    llm, (a, _), sleeps = fallback([err], ["ok"])
    assert llm.call("hi") == "ok"
    assert a.calls == 1 and sleeps == []


def test_malformed_tool_call_retries_same_model_without_wait():
    err = HTTPError(400, "Error code: 400 - {'code': 'tool_use_failed', "
                         "'message': 'Failed to call a function.'}")
    llm, (a, b), sleeps = fallback([err, "ok"], ["slow local"])
    assert llm.call("hi") == "ok"
    assert llm.last_used == "m0" and (a.calls, b.calls) == (2, 0) and sleeps == []
    assert [h["outcome"] for h in llm.history] == ["bad_generation", "ok"]


def test_summarize_history():
    from workforce.tasks import summarize_history

    hist = [{"model": "a", "outcome": "ok", "seconds": 1.0},
            {"model": "a", "outcome": "bad_generation", "seconds": 0.5},
            {"model": "b", "outcome": "ok", "seconds": 60.0}]
    assert summarize_history(hist) == ("a: 2 calls (1 ok, 1 bad_generation), 1.5s; "
                                       "b: 1 calls (1 ok), 60.0s")


def test_auth_error_falls_back_without_retry():
    llm, (a, _), sleeps = fallback([HTTPError(401, "API key not valid")], ["ok"])
    assert llm.call("hi") == "ok"
    assert a.calls == 1 and sleeps == []


def test_all_fail_raises_once_without_outer_rerun():
    llm, (a, b), _ = fallback([HTTPError(429)], [HTTPError(429)])
    with pytest.raises(ChainExhaustedError) as info:
        llm.call("hi")
    # CrewAI wraps call() with its own rate-limit retry; it must NOT re-run the chain.
    assert (a.calls, b.calls) == (3, 3)
    assert {f.kind for f in info.value.failures} == {"quota"}


def test_stop_words_are_passed_to_members():
    llm, (a, _), _ = fallback(["ok"], ["unused"])
    llm.stop = ["\nObservation:"]
    llm.call("hi")
    assert a.stop == ["\nObservation:"]


def test_async_call():
    llm, _, _ = fallback(["async ok"])
    assert asyncio.run(llm.acall("hi")) == "async ok"


@pytest.mark.parametrize(("exc", "kind"), [
    (HTTPError(429), "quota"),
    (Exception("RESOURCE_EXHAUSTED: quota exceeded"), "quota"),
    (HTTPError(403), "auth"),
    (Exception("400 INVALID_ARGUMENT: API key not valid"), "auth"),
    (HTTPError(404, "model 'x' not found"), "not_found"),
    (ConnectionError("refused"), "unreachable"),
    (Exception("APIConnectionError: Connection error."), "unreachable"),
    (ValueError("weird"), "error"),
])
def test_classify_error(exc, kind):
    assert classify_error(exc) == kind


def test_retry_after_parsing():
    assert retry_after_seconds(Exception("'retryDelay': '37s'")) == 37.0
    assert retry_after_seconds(Exception("Retry-After: 5")) == 5.0
    assert retry_after_seconds(Exception("nothing here")) is None


# ---------- candidates / build / privacy ----------

@pytest.fixture
def configured(cfg_root, monkeypatch):
    set_models(cfg_root, cloud_fast="gemini-x", cloud_code="gemini-y",
               local_general="small:3b", local_coder="coder:7b")
    edit_config(cfg_root, "models", lambda d: d["providers"]["gemini"].update(enabled=True))
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    return cfg_root


def names(usable):
    return [c.name for c in usable]


def test_candidates_follow_chain_order(configured):
    usable, skipped = candidates(load_settings(configured), "research")
    assert names(usable) == ["cloud_fast", "local_general"] and skipped == []


def test_local_only_blocks_cloud(configured):
    usable, skipped = candidates(load_settings(configured), "research", local_only=True)
    assert names(usable) == ["local_general"]
    assert skipped == [("cloud_fast", "cloud model blocked by local_only")]


def test_missing_key_skips_cloud(configured, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    usable, skipped = candidates(load_settings(configured), "coding")
    assert names(usable) == ["local_coder"]
    assert skipped == [("cloud_code", "GEMINI_API_KEY not set")]


def test_disabled_provider_skipped(configured):
    edit_config(configured, "models", lambda d: d["providers"]["ollama"].update(enabled=False))
    usable, skipped = candidates(load_settings(configured), "qa")
    assert names(usable) == ["cloud_fast"]
    assert skipped[0][0] == "local_general"


def test_unset_models_give_clear_error(cfg_root):
    with pytest.raises(NoUsableModelError, match="model not chosen"):
        build_llm(load_settings(cfg_root), "research")


def test_unknown_chain_or_model(configured):
    s = load_settings(configured)
    with pytest.raises(KeyError):
        candidates(s, "nope")
    with pytest.raises(KeyError):
        candidates(s, models=["ghost"])


def test_make_llm_builds_native_providers(configured):
    s = load_settings(configured)
    usable, _ = candidates(s, "research")
    gemini, ollama = (make_llm(c) for c in usable)
    assert type(gemini).__name__ == "GeminiCompletion"
    assert type(ollama).__name__ == "OpenAICompatibleCompletion"
    assert ollama.base_url == "http://localhost:11434/v1"


def test_build_llm_explicit_models(configured):
    llm = build_llm(load_settings(configured), models=["local_coder"])
    assert [label for label, _ in llm.members] == ["local_coder (ollama/coder:7b)"]


def test_is_local_only(cfg_root, tmp_path):
    secret_project = tmp_path / "secret-app"
    secret_project.mkdir()
    edit_config(cfg_root, "policies",
                lambda d: d["privacy"].update(local_only_projects=[str(secret_project)]))
    s = load_settings(cfg_root)
    assert is_local_only(s, secret_project / "src")
    assert not is_local_only(s, tmp_path / "public-app")
    assert not is_local_only(s, None)
    edit_config(cfg_root, "policies", lambda d: d["privacy"].update(default_local_only=True))
    assert is_local_only(load_settings(cfg_root), tmp_path / "public-app")
