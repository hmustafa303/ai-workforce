"""End-to-end task flow, offline: a fake CLI engine (a tiny Python script) plays
the worker. First attempt is wrong, the Parent revises, second attempt passes."""

import sys
from pathlib import Path

import pytest
import yaml
from conftest import edit_config, load_settings, make_project

from workforce.engines import Engine
from workforce.engines import registry as engine_registry
from workforce.tasks import EscalateError, TaskError, Workforce

WORKER_SCRIPT = (
    "import pathlib, sys\n"
    "prompt = sys.stdin.read()\n"
    "good = 'Feedback round 1' in prompt\n"
    "pathlib.Path('feature.txt').write_text('good' if good else 'bad')\n"
    "print('I implemented the feature. All tests pass!')\n"  # a claim, not evidence
)
CHECK = ("import pathlib, sys; p = pathlib.Path('feature.txt'); "
         "sys.exit(0 if p.exists() and p.read_text() == 'good' else 1)")


class FakeWriter(Engine):
    def resolve_binary(self):
        return Path(sys.executable)

    def command(self, binary, workdir, *, write, out_file):
        return [str(binary), "-c", WORKER_SCRIPT]


@pytest.fixture
def env(cfg_root, tmp_path, monkeypatch):
    projects = tmp_path / "projects"
    proj = make_project(projects / "app", {
        "README.md": "# app\n",
        ".workforce.yaml": yaml.safe_dump(
            {"checks": [{"name": "feature", "cmd": [sys.executable, "-c", CHECK]}]}),
    })
    (cfg_root / "config" / "engines.yaml").write_text(
        yaml.safe_dump({"engines": {"fake": {"type": "codex", "timeout_seconds": 60}}}))
    edit_config(cfg_root, "agents",
                lambda d: d["agents"]["coder"].__setitem__("engine", "fake"))
    edit_config(cfg_root, "policies", lambda d: (
        d["paths"].__setitem__("allowed_project_roots", [str(projects)]),
        d["tasks"].__setitem__("max_revisions", 1)))
    monkeypatch.setitem(engine_registry.ADAPTERS, "codex", FakeWriter)
    wf = Workforce(load_settings(cfg_root))
    monkeypatch.setattr(wf, "spawn", lambda task_id: 0)  # run in-process instead
    return wf, proj


def _run(wf, task_id):
    assert wf.wait_for_slot(task_id)
    return wf.execute(task_id)


def test_full_loop_revise_verify_merge_cleanup(env):
    wf, proj = env
    t = wf.delegate("coder", "Create feature.txt", project_path=str(proj),
                    acceptance=["feature.txt contains 'good'"])
    assert t.write and t.engine == "fake" and Path(t.worktree).is_dir()

    t = _run(wf, t.id)
    assert t.status == "done" and t.commit_sha
    assert t.verification["passed"] is False  # the claim said "all pass"; the check says no
    info = wf.details(t.id)
    assert info["changed_files"][0]["path"] == "feature.txt"
    assert "All tests pass" in info["worker_report_CLAIM"]
    assert "+bad" in wf.diff(t.id)

    with pytest.raises(TaskError, match="did not pass"):
        wf.merge(t.id, user_confirmed=True)

    wf.revise(t.id, "check 'feature' failed: feature.txt must contain 'good'")
    t = _run(wf, t.id)
    assert t.revision == 1 and t.verification["passed"] is True
    assert "Feedback round 1" in (wf.runs_dir / t.id / "prompt-r1.md").read_text()

    with pytest.raises(EscalateError):
        wf.revise(t.id, "one more")  # max_revisions = 1
    with pytest.raises(TaskError, match="explicit yes"):
        wf.merge(t.id, user_confirmed=False)

    result = wf.merge(t.id, user_confirmed=True)
    assert result["merged"] and (proj / "feature.txt").read_text() == "good"
    assert wf.get(t.id).status == "merged"

    notes = wf.cleanup(t.id)["notes"]
    assert any("deleted branch" in n for n in notes) and not Path(t.worktree).exists()
    assert wf.ledger.stats()[0]["merged"] == 1


def test_merge_refuses_when_code_changed_after_verify(env):
    wf, proj = env
    t = wf.delegate("coder", "x", project_path=str(proj))
    t = _run(wf, t.id)
    Path(t.worktree, "feature.txt").write_text("good")
    from workforce import sandbox

    sandbox.commit_changes(Path(t.worktree), "manual", [])
    with pytest.raises(TaskError, match="did not pass|changed after"):
        wf.merge(t.id, user_confirmed=True)
    assert wf.verify(t.id)["passed"]
    assert wf.merge(t.id, user_confirmed=True)["merged"]


def test_refusals(env, tmp_path, cfg_root):
    wf, proj = env
    with pytest.raises(TaskError, match="unknown role"):
        wf.delegate("nobody", "x")
    with pytest.raises(TaskError, match="project_path is required"):
        wf.delegate("coder", "x")
    other = make_project(tmp_path / "not-allowed")
    with pytest.raises(TaskError, match="allowed_project_roots"):
        wf.delegate("coder", "x", project_path=str(other))
    with pytest.raises(TaskError, match="denied"):
        wf.delegate("coder", "x", project_path=str(proj), context_files=[".env"])
    with pytest.raises(TaskError, match="not found"):
        wf.delegate("coder", "x", project_path=str(proj), context_files=["nope.py"])


def test_local_only_project_refuses_cloud_engines(env, cfg_root):
    wf, proj = env
    edit_config(cfg_root, "policies",
                lambda d: d["privacy"].__setitem__("local_only_projects", [str(proj)]))
    wf2 = Workforce(load_settings(cfg_root))
    with pytest.raises(TaskError, match="local-only"):
        wf2.delegate("coder", "x", project_path=str(proj))


def test_cancel_queued_task(env):
    wf, proj = env
    t = wf.delegate("coder", "x", project_path=str(proj))
    assert wf.cancel(t.id).status == "cancelled"
    assert wf.wait_for_slot(t.id) is False
    with pytest.raises(TaskError, match="cancel it first|already"):
        wf.cancel(t.id)
    assert wf.cleanup(t.id)["cleaned"]


def test_prompt_contains_rules_and_criteria(env):
    wf, proj = env
    t = wf.delegate("coder", "Add a login page", project_path=str(proj), start=False,
                    acceptance=["npm test passes"], context_files=["README.md"])
    prompt = wf.build_prompt(t)
    for needle in ("Add a login page", "- npm test passes", "- README.md",
                   "Never delete, skip or weaken existing tests", "Do not run git commit"):
        assert needle in prompt
