"""Task service: delegate, run, revise, verify, merge, clean up.

Used by both the CLI and the MCP server. A delegated task runs in its own
detached process (`python -m workforce run-task <id>`), so it survives MCP
server restarts and can be cancelled by killing its process tree. The ledger
enforces `policies.tasks.max_concurrent`: extra tasks wait in "queued".

Trust model: a worker's report is a CLAIM. What counts is the commit on the task
branch, which the Parent reads with `diff()` and checks with `verify()`.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

from workforce import sandbox
from workforce.ledger import ACTIVE, Ledger, Task, new_task_id
from workforce.settings import Settings

MAX_INLINE_CONTEXT = 8_000      # chars per context file inlined for CrewAI workers
MAX_INLINE_TOTAL = 30_000
MAX_RESULT_CHARS = 20_000
CREW = "crew"
SEARCH_LOG = "search-urls.txt"   # every URL web_search returned, for source checking


class TaskError(RuntimeError):
    """A request the workforce refuses or cannot do. The message is safe to show."""


class EscalateError(TaskError):
    """The revision limit is reached: the Parent must fix it or ask the user."""


def summarize_history(history: list[dict]) -> str:
    """'model: 12 calls (11 ok, 1 bad_generation), 40.2s' per model, in first-use order."""
    per: dict[str, dict] = {}
    for h in history:
        m = per.setdefault(h["model"], {"calls": 0, "seconds": 0.0, "outcomes": {}})
        m["calls"] += 1
        m["seconds"] += h["seconds"]
        m["outcomes"][h["outcome"]] = m["outcomes"].get(h["outcome"], 0) + 1
    return "; ".join(
        f"{name}: {m['calls']} calls ("
        + ", ".join(f"{n} {k}" for k, n in m["outcomes"].items())
        + f"), {m['seconds']:.1f}s" for name, m in per.items())


# ---------- process helpers ----------

def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        # Never os.kill(pid, 0) on Windows: signal 0 is CTRL_C_EVENT there.
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            return bool(ok) and code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    import os

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def kill_tree(pid: int) -> None:
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(pid)],
                       capture_output=True, check=False, stdin=subprocess.DEVNULL)
    else:
        import os
        import signal

        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
        except OSError:
            pass


# ---------- the service ----------

class Workforce:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        s = self.settings
        self.ledger = Ledger(s.resolve(s.policies.paths.ledger_db))
        self.runs_dir = s.resolve(s.policies.paths.runs_dir)

    # ----- delegate -----

    def delegate(self, role: str, instructions: str, *, project_path: str | None = None,
                 engine: str | None = None, acceptance: list[str] | None = None,
                 context_files: list[str] | None = None, title: str | None = None,
                 start: bool = True) -> Task:
        s = self.settings
        agent = s.agents.agents.get(role)
        if agent is None:
            raise TaskError(f"unknown role '{role}' (roles: {', '.join(s.agents.agents)})")
        if not instructions.strip():
            raise TaskError("instructions are empty")
        engine = engine or agent.engine
        if engine != CREW and engine not in s.engines.engines:
            raise TaskError(f"unknown engine '{engine}' (use 'crew' or one of: "
                            f"{', '.join(s.engines.engines)})")

        project = None
        local_only = s.policies.privacy.default_local_only
        if project_path:
            try:
                project = sandbox.repo_root(sandbox.check_project_allowed(s, project_path))
                sandbox.check_project_allowed(s, project)
            except sandbox.SandboxError as exc:
                raise TaskError(str(exc)) from None
            from workforce.llm.router import is_local_only

            local_only = is_local_only(s, project)
        if local_only and engine != CREW:
            raise TaskError(f"project is local-only (privacy policy): engine '{engine}' is a "
                            f"cloud tool. Use engine='crew', which then runs on Ollama only.")

        write = agent.allow_write and self._engine_can_write(engine)
        if write and project is None:
            raise TaskError(f"role '{role}' writes code, so project_path is required")

        files = list(context_files or [])
        if files and project is None:
            raise TaskError("context_files need a project_path (paths are relative to it)")
        for f in files:
            try:
                if not sandbox.safe_join(project, f, s.policies.paths.denied_globs).is_file():
                    raise TaskError(f"context file not found in the project: {f}")
            except sandbox.SandboxError as exc:
                raise TaskError(str(exc)) from None

        task = Task(
            id=new_task_id(), role=role, engine=engine, status="queued",
            title=(title or instructions.strip().splitlines()[0])[:100],
            instructions=instructions.strip(), acceptance=list(acceptance or []),
            context_files=files, project_path=str(project) if project else None,
            write=write, local_only=local_only)
        warnings = []
        if project is not None:
            dirty = sandbox.dirty_files(project)
            if dirty:
                warnings.append(f"{len(dirty)} uncommitted file(s) in your checkout are NOT "
                                f"visible to the worker (it starts from your last commit)")
            try:
                wt = sandbox.create_worktree(s, project, task.id)
            except sandbox.SandboxError as exc:
                raise TaskError(str(exc)) from None
            task.worktree, task.branch = str(wt.path), wt.branch
            task.base_ref, task.base_branch = wt.base_ref, wt.base_branch
        self.ledger.create(task)
        for w in warnings:
            self.ledger.event(task.id, "warning", w)
        if start:
            self.spawn(task.id)
        return task

    def _engine_can_write(self, engine: str) -> bool:
        if engine == CREW:
            return True
        from workforce.engines import get_engine

        return get_engine(self.settings, engine).supports_write

    # ----- background process -----

    def spawn(self, task_id: str) -> int:
        log_dir = self.runs_dir / task_id
        log_dir.mkdir(parents=True, exist_ok=True)
        cmd = [sys.executable, "-m", "workforce", "run-task", task_id,
               "--root", str(self.settings.root)]
        with open(log_dir / "worker.log", "ab") as log:
            kwargs = dict(cwd=self.settings.root, stdin=subprocess.DEVNULL, stdout=log,
                          stderr=subprocess.STDOUT, close_fds=True)
            if sys.platform == "win32":
                # Hidden console (child CLI tools inherit it: no pop-up windows).
                base = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
                try:
                    proc = subprocess.Popen(
                        cmd, creationflags=base | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                        **kwargs)
                except OSError:  # the parent's job forbids breakaway
                    proc = subprocess.Popen(cmd, creationflags=base, **kwargs)
            else:
                proc = subprocess.Popen(cmd, start_new_session=True, **kwargs)
        self.ledger.event(task_id, "spawned", f"pid {proc.pid}")
        return proc.pid

    def wait_for_slot(self, task_id: str, poll: float = 3.0) -> bool:
        """Block until this task may run. False if it was cancelled meanwhile."""
        import os

        limit = self.settings.policies.tasks.max_concurrent
        while True:
            state = self.ledger.claim_slot(task_id, os.getpid(), limit, is_alive=pid_alive)
            if state == "claimed":
                return True
            if state == "gone":
                return False
            time.sleep(poll)

    def execute(self, task_id: str) -> Task:
        """Run one attempt of a claimed task in THIS process."""
        t = self.ledger.get(task_id)
        s = self.settings
        attempt_dir = self.runs_dir / task_id
        attempt_dir.mkdir(parents=True, exist_ok=True)
        workdir = Path(t.worktree) if t.worktree else attempt_dir / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        self.ledger.event(task_id, "started", f"attempt {t.attempts}, revision {t.revision}")
        started = time.monotonic()
        ok, text, model, error = False, "", None, None
        try:
            prompt = self.build_prompt(t)
            (attempt_dir / f"prompt-r{t.revision}.md").write_text(prompt, encoding="utf-8")
            if t.engine == CREW:
                text, model = self._run_crew(t, prompt, workdir)
                ok = bool(text.strip())
            else:
                ok, text, model, error = self._run_engine(t, prompt, workdir)
        except Exception as exc:  # noqa: BLE001 - record any worker failure in the ledger
            error = f"{type(exc).__name__}: {str(exc)[:1500]}"
        seconds = round(time.monotonic() - started, 1)
        (attempt_dir / f"result-r{t.revision}.md").write_text(
            text or f"(no output)\n\nerror: {error}", encoding="utf-8")

        commit = None
        if t.worktree:
            try:
                commit, skipped = sandbox.commit_changes(
                    workdir, f"workforce {t.id} r{t.revision}: {t.title}",
                    s.policies.paths.denied_globs)
                if skipped:
                    self.ledger.event(task_id, "warning",
                                      f"did not commit denied files: {', '.join(skipped)}")
                self.ledger.event(task_id, "committed", commit or "no changes")
            except sandbox.SandboxError as exc:
                error = (error + "; " if error else "") + str(exc)
                ok = False

        fields = dict(result=text[:MAX_RESULT_CHARS], error=error, model_used=model,
                      seconds=seconds, commit_sha=commit)
        verification = None
        if ok and t.worktree and t.write:
            self.ledger.event(task_id, "verifying")
            verification = self._verify(t)
            fields["verification"] = verification
        fields.update(status="done" if ok else "failed", finished_at=time.time())
        self.ledger.update(task_id, only_if_status=("running",), **fields)
        self.ledger.event(task_id, "finished",
                          f"{'ok' if ok else 'failed'} in {seconds}s"
                          + (f"; checks {verification['summary']}" if verification else ""))
        return self.ledger.get(task_id)

    def _run_engine(self, t: Task, prompt: str, workdir: Path):
        from workforce.engines import get_engine
        from workforce.engines.registry import provider_secret_names

        engine = get_engine(self.settings, t.engine)
        res = engine.run(prompt, workdir, write=t.write,
                         secret_names=provider_secret_names(self.settings),
                         timeout=self.settings.policies.tasks.timeout_seconds)
        error = None
        if not res.ok:
            error = ("timed out" if res.timed_out else f"exit code {res.exit_code}") + \
                    f"; log tail: {res.log_tail[-800:]}"
        label = f"{t.engine}" + (f"/{engine.spec.model}" if engine.spec.model else "")
        return res.ok, res.final_message, label, error

    def _run_crew(self, t: Task, prompt: str, workdir: Path) -> tuple[str, str | None]:
        import logging

        from crewai import Agent, Crew
        from crewai import Task as CrewTask

        # CrewAI warns on every call that token counts are approximate: noise.
        logging.getLogger("crewai.llms.base_llm").setLevel(logging.ERROR)

        from workforce.llm.router import build_llm
        from workforce.tools import build_tools

        s = self.settings
        spec = s.agents.agents[t.role]
        llm = build_llm(s, spec.chain, local_only=t.local_only)
        tools = build_tools(spec.tools, workdir=Path(t.worktree) if t.worktree else None,
                            denied_globs=s.policies.paths.denied_globs, allow_write=t.write,
                            search_log=self.runs_dir / t.id / SEARCH_LOG)
        agent = Agent(role=spec.role, goal=spec.goal, backstory=spec.backstory, llm=llm,
                      tools=tools, allow_delegation=False, verbose=False,
                      max_iter=spec.max_iter,
                      max_execution_time=s.policies.tasks.timeout_seconds)
        expected = ("A concise markdown report. End with a 'Sources' section listing every "
                    "URL you relied on." if not t.write else
                    "A short report: files changed, what was done, what is NOT done, and how "
                    "to verify it.")
        crew_task = CrewTask(description=prompt, expected_output=expected, agent=agent)
        out = Crew(agents=[agent], tasks=[crew_task], verbose=False).kickoff()
        self.ledger.event(t.id, "models", summarize_history(llm.history))
        return str(getattr(out, "raw", out) or ""), llm.last_used

    # ----- prompt -----

    def build_prompt(self, t: Task) -> str:
        spec = self.settings.agents.agents[t.role]
        lines = [
            f"You are the {spec.role} in a supervised AI team. A senior engineer (the "
            f"Parent) assigned you this task and will independently check your work with "
            f"real commands and by reading the git diff.",
            f"Your goal: {spec.goal}", "",
            "## Task", t.instructions, "",
        ]
        if t.acceptance:
            lines += ["## Acceptance criteria (each one will be checked)",
                      *[f"- {a}" for a in t.acceptance], ""]
        if t.context_files:
            lines += ["## Read these files first", *[f"- {f}" for f in t.context_files], ""]
            if t.engine == CREW and t.worktree:
                total = 0
                for f in t.context_files:
                    p = Path(t.worktree) / f
                    if not p.is_file() or total >= MAX_INLINE_TOTAL:
                        continue
                    body = p.read_text(encoding="utf-8", errors="replace")[:MAX_INLINE_CONTEXT]
                    total += len(body)
                    lines += [f"### {f}", "```", body, "```", ""]
        rules = ["Do not read or create secret files (.env, keys, credentials).",
                 "Never invent facts, results or test outcomes. Say what you could not do."]
        if t.worktree:
            rules[:0] = ["Work ONLY inside the current project folder.",
                         "Do not run git commit, git push or change branches: the supervisor "
                         "commits your changes."]
        if t.write:
            rules += ["Follow the project's existing style. Keep the change minimal.",
                      "Add or update tests for the behaviour you change.",
                      "Never delete, skip or weaken existing tests to make them pass."]
        else:
            rules.append("This is a READ-ONLY task: do not modify any file.")
        lines += ["## Rules", *[f"- {r}" for r in rules], ""]

        feedback = self.ledger.feedback(t.id)
        if feedback:
            lines += ["## Revision requested",
                      "Your previous attempt is already in the folder. Fix the problems "
                      "below; do not start over unless told to."]
            for fb in feedback:
                lines += [f"### Feedback round {fb['revision']}", fb["text"], ""]
            if t.result:
                lines += ["### Your previous report (for reference)", t.result[:4000], ""]
        lines += ["## Final answer",
                  "Reply with a short report: what you changed (file list), what you did "
                  "NOT finish, and the exact commands to verify it."
                  if t.write else "Reply with your complete answer."]
        return "\n".join(lines)

    # ----- parent operations -----

    def get(self, task_id: str) -> Task:
        try:
            t = self.ledger.get(task_id)
        except KeyError as exc:
            raise TaskError(str(exc)) from None
        if t.status == "running" and t.pid and not pid_alive(t.pid):
            self.ledger.update(task_id, only_if_status=("running",), status="failed",
                               finished_at=time.time(), error="worker process died")
            t = self.ledger.get(task_id)
        return t

    def details(self, task_id: str) -> dict:
        t = self.get(task_id)
        max_rev = self.settings.policies.tasks.max_revisions
        out = {
            "id": t.id, "status": t.status, "role": t.role, "engine": t.engine,
            "title": t.title, "write": t.write, "local_only": t.local_only,
            "project": t.project_path, "worktree": t.worktree, "branch": t.branch,
            "base_branch": t.base_branch, "base_ref": t.base_ref,
            "revision": f"{t.revision}/{max_rev}", "attempts": t.attempts,
            "model_used": t.model_used, "seconds": t.seconds, "error": t.error,
            "acceptance": t.acceptance,
            "worker_report_CLAIM": (t.result or "")[:8000],
        }
        if t.worktree and Path(t.worktree).is_dir():
            files = sandbox.changed_files(Path(t.worktree), t.base_ref)
            out["changed_files"] = [asdict(f) for f in files]
            out["review_closely"] = [f.path for f in files if f.sensitive]
            out["uncommitted"] = sandbox.uncommitted(Path(t.worktree))
        if t.verification:
            v = t.verification
            out["verification"] = {
                "summary": v.get("summary"), "passed": v.get("passed"),
                "commit": v.get("commit"), "source": v.get("source"),
                "checks": [{"name": c["name"], "cmd": c["cmd"], "passed": c["passed"],
                            "exit_code": c["exit_code"],
                            **({} if c["passed"] else {"output_tail": c["output_tail"][-1500:]})}
                           for c in v.get("checks", []) + v.get("setup", [])],
            }
        out.update(self._quality_checks(t))
        fb = self.ledger.feedback(t.id)
        if fb:
            out["feedback_sent"] = [f["text"][:500] for f in fb]
        out["events"] = [f"{time.strftime('%H:%M:%S', time.localtime(e['ts']))} {e['kind']} "
                         f"{e['detail']}".strip() for e in self.ledger.events(t.id, 12)]
        out["next_step"] = self._next_step(t)
        return out

    def _quality_checks(self, t: Task) -> dict:
        """Cheap deterministic red flags for CrewAI answers."""
        out: dict = {}
        if t.engine != CREW or not t.result:
            return out
        spec = self.settings.agents.agents.get(t.role)
        if spec is None:
            return out
        if "web_search" in spec.tools:
            from workforce.sources import cited_urls, unverified_sources

            cited = cited_urls(t.result)
            log = self.runs_dir / t.id / SEARCH_LOG
            bad = unverified_sources(t.result, log)
            out["source_check"] = {
                "cited_urls": len(cited), "NOT_returned_by_any_search": bad,
                "verdict": ("no sources cited" if not cited else
                            "no search results were recorded for this task, so no cited URL "
                            "can be confirmed" if not log.is_file() else
                            "all cited URLs came from real searches" if not bad else
                            "some cited URLs never appeared in search results: likely "
                            "invented; treat those claims as unverified")}
        first = self.settings.models.chains[spec.chain][0]
        if t.model_used and not t.model_used.startswith(f"{first} "):
            out["quality_warning"] = (
                f"answered by fallback model {t.model_used}, not the primary '{first}' "
                f"(rate limit or error). Small local models are much less reliable: check "
                f"every claim, or revise_task later when the primary model is available.")
        return out

    def _next_step(self, t: Task) -> str:
        if t.status in ACTIVE:
            return "wait, then call get_task again"
        if t.status == "failed":
            return ("read 'error'; revise_task with a fix hint, or do it yourself"
                    if t.revision < self.settings.policies.tasks.max_revisions
                    else "revision limit reached: fix it yourself or ask the user")
        if t.status == "done" and t.write:
            return ("read task_diff, run the checks yourself (verify_task or your own "
                    "commands in the worktree); then revise_task with exact errors, or "
                    "ask the user before merge_task")
        if t.status == "done":
            return "evaluate the report critically; check its sources/claims"
        if t.cleaned_at:
            return "-"
        if t.status == "merged":
            return "cleanup_task to remove the worktree"
        return "-"

    def diff(self, task_id: str, paths: list[str] | None = None,
             max_chars: int = 60_000) -> str:
        t = self.get(task_id)
        if not (t.worktree and Path(t.worktree).is_dir()):
            raise TaskError("this task has no worktree (read-only task without a project, "
                            "or already cleaned up)")
        return sandbox.diff(Path(t.worktree), t.base_ref, paths=paths, max_chars=max_chars) \
            or "(no changes)"

    def _verify(self, t: Task) -> dict:
        from workforce.engines.registry import provider_secret_names
        from workforce.verify import verify

        wt = Path(t.worktree)
        report = verify(wt, Path(t.project_path),
                        timeout=self.settings.policies.tasks.verify_timeout_seconds,
                        secret_names=provider_secret_names(self.settings))
        data = report.to_dict()
        data["summary"] = report.summary()
        data["commit"] = sandbox.git(wt, "rev-parse", "HEAD").stdout.strip()
        return data

    def verify(self, task_id: str) -> dict:
        t = self.get(task_id)
        if t.status in ACTIVE:
            raise TaskError(f"task is {t.status}; wait until it finishes")
        if not (t.worktree and Path(t.worktree).is_dir()):
            raise TaskError("this task has no worktree to verify")
        data = self._verify(t)
        self.ledger.update(task_id, verification=data)
        self.ledger.event(task_id, "verified", data["summary"])
        return data

    def revise(self, task_id: str, feedback: str) -> Task:
        t = self.get(task_id)
        if t.status not in ("done", "failed"):
            raise TaskError(f"cannot revise a task that is {t.status}")
        if not feedback.strip():
            raise TaskError("feedback is empty: send the exact errors and what to change")
        limit = self.settings.policies.tasks.max_revisions
        if t.revision >= limit:
            raise EscalateError(f"revision limit ({limit}) reached for {task_id}: fix it "
                                f"yourself or ask the user")
        rev = t.revision + 1
        self.ledger.add_feedback(task_id, rev, feedback.strip())
        self.ledger.update(task_id, status="queued", revision=rev, finished_at=None,
                           verification=None, error=None)
        self.ledger.event(task_id, "revise", f"round {rev}/{limit}")
        self.spawn(task_id)
        return self.ledger.get(task_id)

    def cancel(self, task_id: str) -> Task:
        t = self.get(task_id)
        if t.status not in ACTIVE:
            raise TaskError(f"task is already {t.status}")
        self.ledger.update(task_id, only_if_status=ACTIVE, status="cancelled",
                           finished_at=time.time())
        if t.pid and pid_alive(t.pid):
            kill_tree(t.pid)
        self.ledger.event(task_id, "cancelled")
        return self.ledger.get(task_id)

    def merge(self, task_id: str, *, user_confirmed: bool,
              allow_unverified: bool = False) -> dict:
        t = self.get(task_id)
        if self.settings.policies.merge.require_user_approval and not user_confirmed:
            raise TaskError("merging needs the user's explicit yes (policies.merge). Ask "
                            "the user, then call again with user_confirmed=true")
        if t.status != "done" or not t.write or not t.worktree:
            raise TaskError(f"only finished write tasks can be merged (this one is "
                            f"{t.status}, write={t.write})")
        wt = Path(t.worktree)
        if sandbox.uncommitted(wt):
            raise TaskError("the worktree has uncommitted changes; verify again first")
        head = sandbox.git(wt, "rev-parse", "HEAD").stdout.strip()
        if head == t.base_ref:
            raise TaskError("the task made no changes; nothing to merge")
        v = t.verification or {}
        if not allow_unverified:
            if not v.get("passed"):
                raise TaskError("last verification did not pass (or never ran). Run "
                                "verify_task; use allow_unverified only if the user agreed")
            if v.get("commit") != head:
                raise TaskError("the code changed after the last verification; run "
                                "verify_task again")
        try:
            sha = sandbox.merge(Path(t.project_path), t.branch, t.base_branch,
                                f"Merge workforce task {t.id}: {t.title}")
        except sandbox.SandboxError as exc:
            raise TaskError(str(exc)) from None
        self.ledger.update(task_id, status="merged", merged_at=time.time())
        self.ledger.event(task_id, "merged", f"{t.branch} -> {t.base_branch} at {sha}")
        return {"merged": True, "into": t.base_branch, "merge_commit": sha,
                "next_step": "cleanup_task to remove the worktree and branch"}

    def cleanup(self, task_id: str, *, delete_unmerged_branch: bool = False) -> dict:
        t = self.get(task_id)
        if t.status in ACTIVE:
            raise TaskError(f"task is {t.status}; cancel it first")
        if t.cleaned_at:
            raise TaskError("task is already cleaned up")
        notes = []
        if t.project_path and (t.worktree or t.branch):
            # A branch with no commits beyond its base holds no work: safe to delete.
            no_work = bool(t.worktree and Path(t.worktree).is_dir() and sandbox.git(
                t.worktree, "rev-parse", "HEAD").stdout.strip() == t.base_ref)
            try:
                notes = sandbox.remove_worktree(
                    Path(t.project_path), Path(t.worktree) if t.worktree else None, t.branch,
                    delete_branch=(t.merged_at is not None or delete_unmerged_branch
                                   or no_work))
            except sandbox.SandboxError as exc:
                raise TaskError(str(exc)) from None
        self.ledger.update(task_id, cleaned_at=time.time())
        self.ledger.event(task_id, "cleaned", "; ".join(notes))
        return {"cleaned": True, "notes": notes}

    def list(self, status: str | None = None, limit: int = 30) -> list[dict]:
        return [{"id": t.id, "status": t.status, "role": t.role, "engine": t.engine,
                 "title": t.title, "revision": t.revision, "model_used": t.model_used,
                 "seconds": t.seconds, "cleaned": t.cleaned_at is not None,
                 "verified": (t.verification or {}).get("summary")}
                for t in self.ledger.list(status, limit)]
