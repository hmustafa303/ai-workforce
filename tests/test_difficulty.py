from pathlib import Path

import pytest
from conftest import edit_config, load_settings, make_project

from workforce.difficulty import classify, escalate
from workforce.engines.codex import CodexEngine
from workforce.settings import EngineSpec, Settings
from workforce.tasks import TaskError, Workforce


@pytest.mark.parametrize("brief,write,tier", [
    ("Fix the typo in README", True, "light"),
    ("Which version of git added worktree?", False, "light"),
    ("Add a divide(a, b) function to calc.py that raises ValueError on zero", True, "standard"),
    ("Add Stripe checkout with webhook handling and auth checks", True, "heavy"),
    ("Refactor the database layer for concurrency and performance", True, "heavy"),
])
def test_classify(brief, write, tier):
    got, reasons = classify(brief, write=write)
    assert got == tier, reasons
    assert reasons


def test_escalate():
    assert escalate("light") == "standard"
    assert escalate("standard") == "heavy"
    assert escalate("heavy") == "heavy"
    assert escalate(None) == "heavy"


def test_real_config_has_tier_chains_and_codex_effort():
    s = Settings.load(load_env=False)
    for chain in ("research", "coding", "uiux", "qa"):
        light, heavy = s.models.chain_for(chain, "light"), s.models.chain_for(chain, "heavy")
        assert light == f"{chain}@light" and heavy == f"{chain}@heavy"
        for c in (chain, light, heavy):
            assert s.models.chain_has_local(c), c
    assert s.models.chain_for("coding", "standard") == "coding"
    assert s.engines.engines["codex"].effort == {"light": "low", "standard": "medium",
                                                 "heavy": "high"}


def test_codex_command_uses_tier_effort_and_model(tmp_path):
    spec = EngineSpec(type="codex", effort={"light": "low", "heavy": "high"},
                      tier_models={"light": "small-model"})
    out = tmp_path / "o.txt"
    light = CodexEngine("codex", spec, "light").command(Path("codex"), tmp_path, write=True,
                                                       out_file=out)
    assert ["--model", "small-model"] == light[light.index("--model"):][:2]
    assert 'model_reasoning_effort="low"' in light
    heavy = CodexEngine("codex", spec, "heavy").command(Path("codex"), tmp_path, write=True,
                                                       out_file=out)
    assert "--model" not in heavy and 'model_reasoning_effort="high"' in heavy
    none = CodexEngine("codex", spec).command(Path("codex"), tmp_path, write=False,
                                              out_file=out)
    assert not any("model_reasoning_effort" in a for a in none)
    assert CodexEngine("codex", spec, "light").label() == "codex (small-model, effort=low)"


@pytest.fixture
def wf(cfg_root, tmp_path, monkeypatch):
    w = Workforce(load_settings(cfg_root))
    monkeypatch.setattr(w, "spawn", lambda task_id: 0)
    return w


def test_delegate_sets_tier_and_revise_escalates(wf, cfg_root, tmp_path):
    proj = make_project(tmp_path / "projects" / "app")
    wf.allow_project(str(proj))
    t = wf.delegate("coder", "Fix the typo in README", project_path=str(proj))
    assert t.tier == "light" and t.tier_reason.startswith("auto:")
    wf.ledger.update(t.id, status="done")
    wf.revise(t.id, "still wrong")
    assert wf.ledger.get(t.id).tier == "standard"
    assert any(e["kind"] == "escalated" for e in wf.ledger.events(t.id))

    t2 = wf.delegate("coder", "x", project_path=str(proj), difficulty="heavy")
    assert t2.tier == "heavy" and t2.tier_reason == "set by the Parent"
    with pytest.raises(TaskError, match="difficulty"):
        wf.delegate("coder", "x", project_path=str(proj), difficulty="huge")


def test_allow_project_edits_config_and_keeps_comments(wf, cfg_root, tmp_path):
    a = make_project(tmp_path / "a")
    b = make_project(tmp_path / "b")
    with pytest.raises(TaskError, match="allowed_project_roots"):
        wf.delegate("research", "x", project_path=str(a))
    assert wf.allow_project(str(a))["allowed"]
    assert wf.allow_project(str(b))["allowed"]
    assert wf.allow_project(str(a / "."))["note"] == "already allowed"
    text = (cfg_root / "config" / "policies.yaml").read_text(encoding="utf-8")
    assert "# Projects that workers are allowed to READ" in text  # comments survive
    roots = [str(p) for p in load_settings(cfg_root).policies.paths.allowed_project_roots]
    assert [Path(r).name for r in roots] == ["a", "b"]
    wf.delegate("research", "x", project_path=str(a), start=False)  # now allowed
    with pytest.raises(TaskError, match="ai-workforce repo itself"):
        wf.allow_project(str(cfg_root))
    with pytest.raises(TaskError, match="not a git repository"):
        (tmp_path / "plain").mkdir()
        wf.allow_project(str(tmp_path / "plain"))


def test_fixture_config_without_tiers_falls_back(cfg_root):
    edit_config(cfg_root, "models", lambda d: None)
    s = load_settings(cfg_root)
    assert s.models.chain_for("research", "heavy") == "research"
