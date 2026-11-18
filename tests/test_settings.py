import pytest
from conftest import edit_config as edit
from conftest import load_settings as load

from workforce.settings import ConfigError, Settings, mask_secret, secret_present


def test_real_config_is_valid():
    s = Settings.load(load_env=False)
    assert set(s.agents.agents) == {"research", "coder", "uiux", "qa", "reviewer"}
    assert s.agents.agents["reviewer"].engine == "cursor"
    assert not s.agents.agents["reviewer"].allow_write
    assert s.policies.merge.require_user_approval is True
    for chain in s.models.chains:
        assert s.models.chain_has_local(chain), f"chain {chain} lacks a local fallback"


def test_agent_with_unknown_chain_rejected(cfg_root):
    edit(cfg_root, "agents", lambda d: d["agents"]["coder"].update(chain="nope"))
    with pytest.raises(ConfigError, match="unknown chain 'nope'"):
        load(cfg_root)


def test_chain_with_unknown_model_rejected(cfg_root):
    edit(cfg_root, "models", lambda d: d["chains"]["coding"].append("ghost"))
    with pytest.raises(ConfigError, match="unknown model 'ghost'"):
        load(cfg_root)


def test_model_with_unknown_provider_rejected(cfg_root):
    edit(cfg_root, "models", lambda d: d["models"]["cloud_fast"].update(provider="acme"))
    with pytest.raises(ConfigError, match="unknown provider 'acme'"):
        load(cfg_root)


def test_unknown_tool_rejected(cfg_root):
    edit(cfg_root, "agents", lambda d: d["agents"]["research"]["tools"].append("shell"))
    with pytest.raises(ConfigError, match="unknown tools"):
        load(cfg_root)


def test_write_tool_requires_allow_write(cfg_root):
    edit(cfg_root, "agents", lambda d: d["agents"]["coder"].update(allow_write=False))
    with pytest.raises(ConfigError, match="allow_write"):
        load(cfg_root)


def test_typo_in_key_rejected(cfg_root):
    edit(cfg_root, "policies", lambda d: d["merge"].update(require_user_aproval=False))
    with pytest.raises(ConfigError, match="require_user_aproval"):
        load(cfg_root)


def test_missing_config_file(cfg_root):
    (cfg_root / "config" / "agents.yaml").unlink()
    with pytest.raises(ConfigError, match="missing config file"):
        load(cfg_root)


def test_invalid_yaml(cfg_root):
    (cfg_root / "config" / "models.yaml").write_text("providers: [unclosed", encoding="utf-8")
    with pytest.raises(ConfigError, match="invalid YAML"):
        load(cfg_root)


def test_secret_present(monkeypatch):
    monkeypatch.setenv("WF_TEST_KEY", "  ")
    assert not secret_present("WF_TEST_KEY")
    monkeypatch.setenv("WF_TEST_KEY", "abc")
    assert secret_present("WF_TEST_KEY")
    monkeypatch.delenv("WF_TEST_KEY")
    assert not secret_present("WF_TEST_KEY")


def test_mask_secret():
    assert mask_secret(None) == "<empty>"
    assert mask_secret("short") == "****"
    assert mask_secret("AIzaSyVeryLongSecret1234") == "****1234"


def test_settings_never_hold_secret_values(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-THIS-MUST-NOT-LEAK")
    s = Settings.load(load_env=False)
    assert "THIS-MUST-NOT-LEAK" not in repr(s)
    assert "THIS-MUST-NOT-LEAK" not in s.model_dump_json()


def test_ollama_base_url_env_override(monkeypatch):
    s = Settings.load(load_env=False)
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://127.0.0.1:9999/")
    assert s.models.providers["ollama"].resolved_base_url() == "http://127.0.0.1:9999"
    monkeypatch.delenv("OLLAMA_BASE_URL")
    assert s.models.providers["ollama"].resolved_base_url() == "http://localhost:11434"
