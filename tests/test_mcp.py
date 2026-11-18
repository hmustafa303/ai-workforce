import asyncio
import json

from conftest import load_settings

from workforce import mcp_server
from workforce.tasks import Workforce

EXPECTED = {"workforce_doctor", "list_roles", "delegate_task", "get_task", "task_diff",
            "verify_task", "revise_task", "list_tasks", "cancel_task", "merge_task",
            "cleanup_task", "allow_project", "wait_task"}


def _call(name, args):
    result = asyncio.run(mcp_server.mcp.call_tool(name, args))
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def test_tools_and_prompts_registered():
    tools = {t.name for t in asyncio.run(mcp_server.mcp.list_tools())}
    assert EXPECTED <= tools
    prompts = {p.name for p in asyncio.run(mcp_server.mcp.list_prompts())}
    assert {"delegate", "verify"} <= prompts
    assert "ASK THE USER before" in mcp_server.INSTRUCTIONS


def test_errors_come_back_as_data(cfg_root, monkeypatch):
    wf = Workforce(load_settings(cfg_root))
    monkeypatch.setattr(mcp_server, "_wf", wf)
    monkeypatch.setattr(mcp_server, "_cfg_stamp", mcp_server._config_stamp())
    assert mcp_server._workforce() is wf  # no reload while config is unchanged
    out = _call("allow_project", {"project_path": str(cfg_root)})
    assert "ask the user" in out["error"]
    out = _call("delegate_task", {"role": "coder", "instructions": "x"})
    assert "project_path is required" in out["error"]
    out = _call("get_task", {"task_id": "nope"})
    assert "no task" in out["error"]
    out = _call("merge_task", {"task_id": "nope"})
    assert "error" in out
    roles = _call("list_roles", {})
    assert roles["roles"]["coder"]["writes_code"] is True
    assert roles["roles"]["research"]["writes_code"] is False


def test_wait_task_returns_compact_status(cfg_root, monkeypatch):
    from workforce.ledger import Task, new_task_id

    wf = Workforce(load_settings(cfg_root))
    monkeypatch.setattr(mcp_server, "_wf", wf)
    monkeypatch.setattr(mcp_server, "_cfg_stamp", mcp_server._config_stamp())
    done = wf.ledger.create(Task(id=new_task_id(), role="research", engine="crew",
                                 status="done", title="t", instructions="x",
                                 verification={"summary": "PASSED: pytest=pass"}))
    out = _call("wait_task", {"task_ids": [done.id], "max_wait_seconds": 30})
    assert out["tasks"][done.id]["status"] == "done" and not out["still_running"]
    assert out["tasks"][done.id]["checks"] == "PASSED: pytest=pass"

    running = wf.ledger.create(Task(id=new_task_id(), role="research", engine="crew",
                                    status="queued", title="t", instructions="x"))
    out = _call("wait_task", {"task_ids": [done.id, running.id], "max_wait_seconds": 1})
    assert out["still_running"] == [running.id]
    assert "error" in _call("wait_task", {"task_ids": ["nope"]})
