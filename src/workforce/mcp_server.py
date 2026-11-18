"""The `workforce` MCP server: Claude Code (the Parent) delegates to child workers.

Run with `python -m workforce mcp` (stdio). Nothing may be printed to stdout
here except MCP protocol messages; workers run in their own processes.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from workforce.settings import TIERS
from workforce.tasks import TaskError, Workforce

INSTRUCTIONS = """\
You are the PARENT of an AI workforce. Child workers (CrewAI agents on Groq/Ollama,
the Codex CLI which can write code, the Cursor CLI which is read-only) do the bulk work.
Loop for every request:
1. Understand: restate the requirement; write acceptance criteria a command can check.
2. Plan: split into tasks, pick a role per task (list_roles). Delegate the bulk work (code,
   tests, research, reviews) to save your own tokens; do tiny or sensitive work yourself.
   Difficulty is picked automatically (light/standard/heavy); override it when you know better.
   A project must be allowed first: if delegate_task says so, ask the user, then allow_project.
3. Delegate: delegate_task with precise instructions, context_files and acceptance_criteria.
   Tasks run in the background; poll get_task (local models can take minutes).
4. Verify independently: the worker's report is only a CLAIM. Read task_diff, look hard at
   'review_closely' files (tests/config changed?), run the checks yourself (verify_task, or
   your own commands inside the task worktree; use Playwright for UI).
5. Correct: on failure, revise_task with the EXACT errors (bounded rounds). At the limit,
   fix it yourself or ask the user.
6. Approve: summarize changes + evidence (commands and results). ASK THE USER before
   merge_task. Never set user_confirmed=true without the user's explicit yes in this chat.
Never read or send secret files (.env, keys). Never call anything verified that you did not
check yourself in this session.
"""

mcp = FastMCP("workforce", instructions=INSTRUCTIONS)
_wf: Workforce | None = None
_cfg_stamp: float = 0.0


def _config_stamp() -> float:
    from workforce.settings import PROJECT_ROOT

    files = list((PROJECT_ROOT / "config").glob("*.yaml"))
    return max((f.stat().st_mtime for f in files), default=0.0)


def _workforce() -> Workforce:
    """The service, reloaded whenever a config/*.yaml file changed."""
    global _wf, _cfg_stamp
    stamp = _config_stamp()
    if _wf is None or stamp != _cfg_stamp:
        _wf, _cfg_stamp = Workforce(), stamp
    return _wf


def _safe(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except TaskError as exc:
        return {"error": str(exc), "escalate": type(exc).__name__ == "EscalateError"}


@mcp.tool()
def workforce_doctor(live: bool = False) -> dict:
    """Health of the workforce: config, providers (Groq/Ollama), models, engines, ledger,
    safety. live=true also sends a tiny real prompt to each cloud provider."""
    from workforce.doctor import FAIL, run_checks

    checks = run_checks(live=live)
    return {"ok": not any(c.status == FAIL for c in checks),
            "checks": [f"[{c.status}] {c.name}: {c.detail}" for c in checks]}


@mcp.tool()
def list_roles() -> dict:
    """Worker roles you can delegate to, with their default engine, model chain and whether
    they can write code."""
    wf = _workforce()
    s = wf.settings
    roles = {}
    for name, a in s.agents.agents.items():
        chain = " -> ".join(f"{s.models.models[m].provider}/{s.models.models[m].model}"
                            for m in s.models.chains[a.chain])
        tiers = {}
        for tier in TIERS:
            c = s.models.chain_for(a.chain, tier)
            tiers[tier] = " -> ".join(s.models.models[m].model or "?"
                                      for m in s.models.chains[c])
        roles[name] = {"role": a.role, "goal": a.goal, "default_engine": a.engine,
                       "writes_code": a.allow_write and wf._engine_can_write(a.engine),
                       "crew_chain": chain, "crew_models_by_difficulty": tiers}
    engines = {n: {"type": e.type, "enabled": e.enabled,
                   "can_write": wf._engine_can_write(n),
                   "effort_by_difficulty": dict(e.effort),
                   "model": e.model or "plan default"} for n, e in s.engines.engines.items()}
    return {"roles": roles, "engines": {"crew": {"can_write": True}, **engines},
            "allowed_project_roots": [str(p) for p in s.policies.paths.allowed_project_roots],
            "max_revisions": s.policies.tasks.max_revisions}


@mcp.tool()
def delegate_task(role: str, instructions: str, project_path: str | None = None,
                  acceptance_criteria: list[str] | None = None,
                  context_files: list[str] | None = None, engine: str | None = None,
                  difficulty: str = "auto", title: str | None = None) -> dict:
    """Start a worker task in the background. Returns the task id immediately.

    role: e.g. research, coder, uiux, qa, reviewer (see list_roles).
    project_path: the git repo to work on (must be in allowed_project_roots). Writing roles
      get their own git worktree + branch from the project's last commit.
    acceptance_criteria: checkable statements, ideally commands that must pass.
    context_files: paths relative to project_path that the worker should read first.
    engine: override the role's default: 'crew' (Groq/Ollama), 'codex' (writes), 'cursor'
      (read-only review).
    difficulty: 'auto' (default: rules pick light/standard/heavy from the brief), or set it.
      light = small fast model / low reasoning effort, heavy = biggest model / high effort.
      Each revise_task round moves one tier up automatically."""
    def run():
        t = _workforce().delegate(role, instructions, project_path=project_path,
                                  engine=engine, acceptance=acceptance_criteria,
                                  context_files=context_files, title=title,
                                  difficulty=difficulty)
        return {"task_id": t.id, "status": t.status, "engine": t.engine, "write": t.write,
                "difficulty": t.tier, "difficulty_why": t.tier_reason,
                "worktree": t.worktree, "branch": t.branch,
                "next_step": "poll get_task(task_id) until status is done or failed"}
    return _safe(run)


@mcp.tool()
def get_task(task_id: str) -> dict:
    """Status, the worker's report (a CLAIM), changed files, files to review closely,
    last verification results, events and the suggested next step."""
    return _safe(_workforce().details, task_id)


@mcp.tool()
def task_diff(task_id: str, paths: list[str] | None = None, max_chars: int = 60000) -> dict:
    """The real git diff of the task branch against its starting commit (optionally only
    some paths). This, not the worker's report, is what the worker actually did."""
    return _safe(lambda: {"diff": _workforce().diff(task_id, paths, max_chars)})


@mcp.tool()
def verify_task(task_id: str) -> dict:
    """Run the project's checks (tests/lint/build from .workforce.yaml in YOUR checkout, or
    auto-detected) inside the task worktree. Deterministic: exit codes, not opinions.
    Can take minutes (installs, test suites)."""
    def run():
        v = _workforce().verify(task_id)
        return {"passed": v["passed"], "summary": v["summary"], "commit": v["commit"],
                "source": v["source"], "note": v.get("note", ""),
                "checks": [{"name": c["name"], "cmd": c["cmd"], "passed": c["passed"],
                            "exit_code": c["exit_code"], "seconds": c["seconds"],
                            "output_tail": c["output_tail"][-2500:]}
                           for c in v["setup"] + v["checks"]]}
    return _safe(run)


@mcp.tool()
def revise_task(task_id: str, feedback: str) -> dict:
    """Send corrections back to the same worker (same worktree, previous work kept).
    Include the exact failing commands and error output. Bounded by max_revisions."""
    def run():
        t = _workforce().revise(task_id, feedback)
        return {"task_id": t.id, "status": t.status, "revision": t.revision,
                "next_step": "poll get_task"}
    return _safe(run)


@mcp.tool()
def list_tasks(status: str | None = None, limit: int = 30) -> dict:
    """Recent tasks from the ledger (optionally filtered by status: queued, running, done,
    failed, cancelled, merged)."""
    return _safe(lambda: {"tasks": _workforce().list(status, limit)})


@mcp.tool()
def allow_project(project_path: str, user_confirmed: bool = False) -> dict:
    """Let workers work on a git project (adds it to allowed_project_roots).
    Ask the user first ("May the workforce work on <path>?"); only then user_confirmed=true."""
    if not user_confirmed:
        return {"error": "ask the user whether the workforce may work on this project, then "
                         "call again with user_confirmed=true"}
    return _safe(_workforce().allow_project, project_path)


@mcp.tool()
def cancel_task(task_id: str) -> dict:
    """Stop a queued or running task (kills the worker's whole process tree)."""
    return _safe(lambda: {"status": _workforce().cancel(task_id).status})


@mcp.tool()
def merge_task(task_id: str, user_confirmed: bool = False,
               allow_unverified: bool = False) -> dict:
    """Merge the task branch into the user's branch (git merge --no-ff).
    ONLY after the user explicitly said yes in this conversation: then user_confirmed=true.
    Refuses if the last verification failed or is older than the current code, unless
    allow_unverified=true (also only with the user's explicit agreement)."""
    return _safe(_workforce().merge, task_id, user_confirmed=user_confirmed,
                 allow_unverified=allow_unverified)


@mcp.tool()
def cleanup_task(task_id: str, delete_unmerged_branch: bool = False) -> dict:
    """Remove the task's worktree. Its branch is deleted only if merged, or if
    delete_unmerged_branch=true (that throws the worker's work away)."""
    return _safe(_workforce().cleanup, task_id,
                 delete_unmerged_branch=delete_unmerged_branch)


# ---------- prompts (appear as slash commands in Claude Code) ----------

@mcp.prompt()
def delegate(request: str, project_path: str = "") -> str:
    """Plan and delegate a request to the workforce, then verify it."""
    where = f" in the project {project_path}" if project_path else ""
    return (f"Handle this request{where} with the workforce:\n\n{request}\n\n"
            "Follow the parent loop: restate it, write command-checkable acceptance "
            "criteria, call list_roles, delegate tasks, poll get_task, then verify "
            "independently (task_diff + verify_task + your own checks), revise with exact "
            "errors if needed, and ask me before any merge.")


@mcp.prompt()
def verify(task_id: str) -> str:
    """Independently verify a finished workforce task."""
    return (f"Independently verify workforce task {task_id}: get_task, read the full "
            "task_diff (look hard at review_closely files), run verify_task and any extra "
            "checks the acceptance criteria need, then give me a verdict with evidence. "
            "Do not merge.")


def _isolate_stdin() -> None:
    """Keep the protocol pipe away from child processes.

    On Windows, while the server thread blocks reading stdin, any child process
    that inherits that pipe (git, or a subprocess some library starts) hangs in
    CreateProcess. So: move the protocol pipe to a private fd for the MCP reader,
    and point the process-wide standard input (fd 0 / STD_INPUT_HANDLE) at NUL.
    """
    import io
    import os
    import sys

    proto = os.dup(0)
    devnull = os.open(os.devnull, os.O_RDONLY)
    os.dup2(devnull, 0)  # also updates STD_INPUT_HANDLE on Windows
    os.close(devnull)
    sys.stdin = io.TextIOWrapper(io.BufferedReader(io.FileIO(proto, "rb")), encoding="utf-8")


def main() -> None:
    _isolate_stdin()
    mcp.run()  # stdio
