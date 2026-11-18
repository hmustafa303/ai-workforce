"""Doctor tests are hermetic: provider network probes are replaced with fakes,
and they run against the frozen config in tests/fixtures/config."""

import pytest
from conftest import edit_config, set_models

from workforce.cli import main
from workforce.doctor import FAIL, OK, WARN, run_checks
from workforce.llm import providers


@pytest.fixture
def fake_providers(monkeypatch):
    state = {"gemini": ["gemini-x", "gemini-y"], "ollama": ["qwen-test:3b"],
             "openai": ["oss-test"], "ollama_up": True, "probe_ok": True}

    def gemini_list(key):
        if state["gemini"] is None:
            raise providers.ProviderError("HTTP 400 INVALID_ARGUMENT: API key not valid")
        return state["gemini"]

    def need_ollama():
        if not state["ollama_up"]:
            raise providers.ProviderError("unreachable: connection refused")

    def ollama_version(url):
        need_ollama()
        return "9.9.9"

    def ollama_list(url):
        need_ollama()
        return state["ollama"]

    def probe(provider, key, model):
        if state["probe_ok"]:
            return providers.ProbeResult(model, True, 0.1, "PONG")
        return providers.ProbeResult(model, False, 0.1, "HTTP 403 PERMISSION_DENIED: no free tier")

    monkeypatch.setattr(providers, "gemini_list_models", gemini_list)
    monkeypatch.setattr(providers, "ollama_version", ollama_version)
    monkeypatch.setattr(providers, "ollama_list_models", ollama_list)
    monkeypatch.setattr(providers, "openai_list_models", lambda base, key: state["openai"])
    monkeypatch.setattr(providers, "probe", probe)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-test-key")
    monkeypatch.setenv("GROQ_API_KEY", "fake-test-key")
    return state


def by_name(checks, name):
    return [c for c in checks if c.name == name]


def test_baseline_has_no_failures(fake_providers, cfg_root):
    checks = run_checks(cfg_root)
    failures = [c for c in checks if c.status == FAIL]
    assert not failures, failures
    names = {c.name for c in checks}
    assert {"python", "package:crewai", "package:mcp", "config", "git"} <= names
    assert by_name(checks, "secrets:gitignore")[0].status == OK


def test_real_project_config_and_secrets_safety(fake_providers):
    checks = run_checks(live=False)
    assert by_name(checks, "config")[0].status == OK
    assert by_name(checks, "git")[0].status == OK
    assert by_name(checks, "secrets:gitignore")[0].status == OK
    assert by_name(checks, "secrets:tracked") == []


def test_doctor_reports_config_errors(cfg_root):
    (cfg_root / "config" / "agents.yaml").write_text("agents: {}", encoding="utf-8")
    checks = run_checks(cfg_root)
    assert any(c.name == "config" and c.status == FAIL for c in checks)


def test_env_not_ignored_is_a_failure(fake_providers, cfg_root):
    (cfg_root / ".gitignore").write_text("", encoding="utf-8")
    assert by_name(run_checks(cfg_root), "secrets:gitignore")[0].status == FAIL


def test_valid_gemini_key(fake_providers, cfg_root):
    gemini = by_name(run_checks(cfg_root), "provider:gemini")[0]
    assert gemini.status == OK and "set and valid" in gemini.detail


def test_rejected_gemini_key_is_a_failure(fake_providers, cfg_root):
    fake_providers["gemini"] = None
    gemini = by_name(run_checks(cfg_root), "provider:gemini")[0]
    assert gemini.status == FAIL
    assert "API key not valid" in gemini.detail


def test_missing_gemini_key_is_a_warning(fake_providers, cfg_root, monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert by_name(run_checks(cfg_root), "provider:gemini")[0].status == WARN


def test_disabled_provider_is_not_probed(fake_providers, cfg_root):
    edit_config(cfg_root, "models", lambda d: d["providers"]["gemini"].update(enabled=False))
    fake_providers["gemini"] = None  # would FAIL if it were probed
    assert by_name(run_checks(cfg_root), "provider:gemini") == []


def test_live_probe_catches_valid_key_without_quota(fake_providers, cfg_root):
    set_models(cfg_root, cloud_fast="gemini-x")
    fake_providers["probe_ok"] = False
    live = by_name(run_checks(cfg_root), "live:gemini")[0]
    assert live.status == FAIL and "403" in live.detail
    assert by_name(run_checks(cfg_root, live=False), "live:gemini") == []


def test_openai_compatible_provider_checked(fake_providers, cfg_root):
    edit_config(cfg_root, "models", lambda d: d["providers"]["groq"].update(enabled=True))
    groq = by_name(run_checks(cfg_root), "provider:groq")[0]
    assert groq.status == OK and "GROQ_API_KEY" in groq.detail


def test_stopped_ollama_is_a_warning(fake_providers, cfg_root):
    fake_providers["ollama_up"] = False
    assert by_name(run_checks(cfg_root), "provider:ollama")[0].status == WARN


def test_configured_model_must_exist(fake_providers, cfg_root):
    set_models(cfg_root, cloud_fast="gemini-x", cloud_code="gemini-gone",
               local_general="qwen-test:3b")
    checks = run_checks(cfg_root)
    assert by_name(checks, "model:cloud_fast")[0].status == OK
    assert by_name(checks, "model:local_general")[0].status == OK
    missing = by_name(checks, "model:cloud_code")[0]
    assert missing.status == FAIL and "gemini-gone" in missing.detail


def test_ollama_latest_tag_is_implied(fake_providers, cfg_root):
    fake_providers["ollama"] = ["llama-test:latest"]
    set_models(cfg_root, local_general="llama-test")
    assert by_name(run_checks(cfg_root), "model:local_general")[0].status == OK


def test_cli_doctor_never_prints_secrets(fake_providers, monkeypatch, capsys):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-SECRET-VALUE-DO-NOT-PRINT")
    monkeypatch.setenv("GROQ_API_KEY", "gsk-SECRET-VALUE-DO-NOT-PRINT")
    main(["doctor", "--quick"])
    assert "SECRET-VALUE" not in capsys.readouterr().out


def test_cli_config_lists_agents(capsys):
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    for agent in ("research", "coder", "uiux", "qa"):
        assert agent in out
