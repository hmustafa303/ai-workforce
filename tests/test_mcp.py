import asyncio
import json

from conftest import load_settings

from workforce import mcp_server
from workforce.tasks import Workforce

EXPECTED = {"workforce_doctor", "list_roles", "delegate_task", "get_task", "task_diff",
            "verify_task", "revise_task", "list_tasks", "cancel_task", "merge_task",
            "cleanup_task", "allow_project"}


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
