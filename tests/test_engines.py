"""CLI engine tests. A fake engine runs a tiny Python script instead of a real
AI tool, so these are offline and fast."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import load_settings

from workforce.engines import Engine, EngineUnavailable, child_env, get_engine
from workforce.engines.codex import CodexEngine
from workforce.engines.cursor import CursorEngine
from workforce.engines.registry import provider_secret_names
from workforce.settings import EngineSpec

ECHO_SCRIPT = (
    "import os, sys\n"
    "prompt = sys.stdin.read()\n"
    "secrets = sorted(k for k in os.environ if k in ('GROQ_API_KEY','MY_TOKEN','CUSTOM_SECRET'))\n"
    "print('prompt=' + prompt)\n"
    "print('secrets=' + ','.join(secrets))\n"
)


class FakeEngine(Engine):
    def __init__(self, script: str, **spec):
        super().__init__("fake", EngineSpec(type="codex", **spec))
        self.script = script

    def resolve_binary(self) -> Path:
        return Path(sys.executable)

    def command(self, binary, workdir, *, write, out_file):
        return [str(binary), "-c", self.script]


def test_child_env_strips_secrets(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "x")
    monkeypatch.setenv("MY_TOKEN", "x")
    monkeypatch.setenv("CUSTOM_SECRET", "x")
    monkeypatch.setenv("ODD_NAME", "x")
    monkeypatch.setenv("HARMLESS", "keep")
    env = child_env({"odd_name"})
    assert env["HARMLESS"] == "keep"
    for name in ("GROQ_API_KEY", "MY_TOKEN", "CUSTOM_SECRET", "ODD_NAME"):
        assert name not in env


def test_run_feeds_prompt_on_stdin_and_hides_secrets(monkeypatch, tmp_path):
    monkeypatch.setenv("GROQ_API_KEY", "should-not-leak")
    monkeypatch.setenv("MY_TOKEN", "should-not-leak")
    result = FakeEngine(ECHO_SCRIPT).run("hello worker", tmp_path)
    assert result.ok and result.exit_code == 0
    assert "prompt=hello worker" in result.final_message
    assert "secrets=\n" in result.final_message + "\n"  # none visible to the child
    assert "should-not-leak" not in result.log_tail


def test_run_uses_workdir_as_cwd(tmp_path):
    result = FakeEngine("import os; print(os.getcwd())").run("", tmp_path)
    assert Path(result.final_message).resolve() == tmp_path.resolve()


def test_timeout_kills_process(tmp_path):
    result = FakeEngine("import time; time.sleep(30)").run("", tmp_path, timeout=1)
    assert result.timed_out and not result.ok
    assert result.seconds < 15


def test_nonzero_exit_is_failure(tmp_path):
    result = FakeEngine("import sys; print('boom'); sys.exit(3)").run("", tmp_path)
    assert not result.ok and result.exit_code == 3


def test_disabled_engine_refuses(tmp_path):
    with pytest.raises(EngineUnavailable, match="disabled"):
        FakeEngine("print(1)", enabled=False).run("", tmp_path)


def test_missing_workdir_refuses(tmp_path):
    with pytest.raises(EngineUnavailable, match="workdir"):
        FakeEngine("print(1)").run("", tmp_path / "nope")


def test_codex_command_is_sandboxed(tmp_path):
    engine = CodexEngine("codex", EngineSpec(type="codex", model="m1"))
    out = tmp_path / "out.txt"
    ro = engine.command(Path("codex.exe"), tmp_path, write=False, out_file=out)
    rw = engine.command(Path("codex.exe"), tmp_path, write=True, out_file=out)
    assert ro[ro.index("--sandbox") + 1] == "read-only"
    assert rw[rw.index("--sandbox") + 1] == "workspace-write"
    for cmd in (ro, rw):
        assert cmd[cmd.index("--cd") + 1] == str(tmp_path)
        assert cmd[-1] == "-"                      # prompt via stdin
        assert cmd[cmd.index("--model") + 1] == "m1"
        assert not any("dangerously" in part or part == "danger-full-access" for part in cmd)


def test_codex_final_message_from_file(tmp_path):
    engine = CodexEngine("codex", EngineSpec(type="codex"))
    out = tmp_path / "last.txt"
    assert engine.final_message("noise", out) == ""
    out.write_text("  DONE \n", encoding="utf-8")
    assert engine.final_message("noise", out) == "DONE"


def test_codex_explicit_binary_must_exist(tmp_path):
    engine = CodexEngine("codex", EngineSpec(type="codex", binary=str(tmp_path / "x.exe")))
    with pytest.raises(EngineUnavailable, match="not found"):
        engine.resolve_binary()


def test_cursor_is_read_only(tmp_path):
    engine = CursorEngine("cursor", EngineSpec(type="cursor", model="m2"))
    cmd = engine.command(Path("node.exe"), tmp_path, write=True, out_file=tmp_path / "o")
    assert cmd[cmd.index("--mode") + 1] == "ask"
    assert "--force" not in cmd and "--yolo" not in cmd
    assert cmd[cmd.index("--workspace") + 1] == str(tmp_path)
    assert cmd[cmd.index("--model") + 1] == "m2"
    with pytest.raises(EngineUnavailable, match="read-only"):
        engine.run("x", tmp_path, write=True)


def test_cursor_parses_json_result(tmp_path):
    engine = CursorEngine("cursor", EngineSpec(type="cursor"))
    ok = 'noise\n{"type":"result","is_error":false,"result":" PONG "}\n'
    err = '{"type":"result","is_error":true,"result":"boom"}'
    assert engine.final_message(ok, tmp_path) == "PONG"
    assert engine.final_message(err, tmp_path) == ""
    assert engine.final_message("not json", tmp_path) == ""


def test_cursor_permission_file_is_temporary_and_restored(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    engine = CursorEngine("cursor", EngineSpec(type="cursor"))
    perm = tmp_path / ".cursor" / "cli.json"

    engine.prepare(tmp_path, write=False)
    rules = json.loads(perm.read_text(encoding="utf-8"))["permissions"]
    assert "Shell(*)" in rules["deny"] and "Write(**)" in rules["deny"]
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=tmp_path, capture_output=True, text=True).stdout
    assert ".cursor" not in status  # excluded via info/exclude
    engine.cleanup(tmp_path)
    assert not (tmp_path / ".cursor").exists()

    perm.parent.mkdir()
    perm.write_text('{"project": "own settings"}', encoding="utf-8")
    engine.prepare(tmp_path, write=False)
    engine.cleanup(tmp_path)
    assert perm.read_text(encoding="utf-8") == '{"project": "own settings"}'


def test_registry(cfg_root):
    (cfg_root / "config" / "engines.yaml").write_text(
        "engines:\n  codex: {type: codex}\n  cursor: {type: cursor, enabled: false}\n",
        encoding="utf-8")
    s = load_settings(cfg_root)
    assert isinstance(get_engine(s, "codex"), CodexEngine)
    with pytest.raises(EngineUnavailable, match="unknown engine"):
        get_engine(s, "nope")
    assert "GEMINI_API_KEY" in provider_secret_names(s)


def test_engines_file_is_optional(cfg_root):
    assert load_settings(cfg_root).engines.engines == {}
