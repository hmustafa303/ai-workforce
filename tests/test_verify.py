import json
import sys

import yaml

from workforce.verify import detect, verify


def _cfg(path, checks, setup=None):
    data = {"checks": checks}
    if setup:
        data["setup"] = setup
    (path / ".workforce.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")


def test_detect_node_scripts(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {
        "test": 'echo "Error: no test specified" && exit 1', "lint": "eslint .",
        "build": "vite build", "dev": "vite"}}))
    source, setup, checks = detect(tmp_path)
    assert source == "auto:node"
    assert [c.name for c in checks] == ["lint", "build"]  # default npm test stub skipped
    assert setup[0].cmd[-1] == "install"


def test_detect_python(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.ruff]\nline-length = 100\n")
    (tmp_path / "tests").mkdir()
    source, _, checks = detect(tmp_path)
    assert source == "auto:python" and [c.name for c in checks] == ["pytest", "ruff"]


def test_nothing_to_check_is_not_a_pass(tmp_path):
    report = verify(tmp_path, tmp_path)
    assert not report.passed and report.source == "none"
    assert report.summary().startswith("NOT VERIFIED")


def test_config_comes_from_checkout_not_worktree(tmp_path):
    checkout, worktree = tmp_path / "checkout", tmp_path / "wt"
    checkout.mkdir()
    worktree.mkdir()
    py = sys.executable
    _cfg(checkout, [{"name": "has-file",
                     "cmd": [py, "-c", "import pathlib,sys; "
                             "sys.exit(0 if pathlib.Path('ok.txt').exists() else 3)"]}])
    # A worker "weakening" the checks in its worktree has no effect.
    _cfg(worktree, [{"name": "always", "cmd": [py, "-c", "pass"]}])
    report = verify(worktree, checkout)
    assert not report.passed and report.checks[0].exit_code == 3
    (worktree / "ok.txt").write_text("x")
    report = verify(worktree, checkout)
    assert report.passed and report.summary() == "PASSED: has-file=pass"


def test_failed_setup_stops_and_secrets_are_hidden(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "leak-me")
    py = sys.executable
    _cfg(tmp_path, [{"name": "env", "cmd": [py, "-c",
                     "import os,sys; sys.exit(9 if 'GROQ_API_KEY' in os.environ else 0)"]}])
    assert verify(tmp_path, tmp_path).passed
    _cfg(tmp_path, [{"name": "x", "cmd": [py, "-c", "pass"]}],
         setup=[[py, "-c", "import sys; sys.exit(1)"]])
    report = verify(tmp_path, tmp_path)
    assert not report.passed and "setup" in report.note and not report.checks


def test_timeout(tmp_path):
    py = sys.executable
    _cfg(tmp_path, [{"name": "slow", "cmd": [py, "-c", "import time; time.sleep(30)"]}])
    report = verify(tmp_path, tmp_path, timeout=1)
    assert not report.passed and report.checks[0].timed_out
