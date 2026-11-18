"""Command line entry point: `workforce <command>`."""

from __future__ import annotations

import argparse
import json
import os
import time
from dataclasses import asdict

from workforce import __version__

SMOKE_PROMPT = "Reply with exactly the single word: PONG"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workforce", description="AI Workforce control CLI")
    parser.add_argument("--version", action="version", version=f"workforce {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser("doctor", help="check environment, config, providers and safety")
    doctor.add_argument("--json", action="store_true", help="machine-readable output")
    doctor.add_argument("--quick", action="store_true",
                        help="skip the live test prompt to cloud providers")

    sub.add_parser("config", help="show agents -> chains -> models")
    sub.add_parser("models", help="list models each provider actually offers")

    smoke = sub.add_parser("smoke", help="send a tiny prompt through a chain or one model")
    target = smoke.add_mutually_exclusive_group(required=True)
    target.add_argument("--chain", help="fallback chain name, e.g. research")
    target.add_argument("--model", help="single logical model, e.g. cloud_fast")
    smoke.add_argument("--local-only", action="store_true", help="block cloud models")
    smoke.add_argument("--break-cloud", action="store_true",
                       help="use an invalid cloud key for this run (tests fallback)")

    probe = sub.add_parser("probe", help="send a tiny real prompt to specific model ids")
    probe.add_argument("provider", help="provider name from models.yaml, e.g. groq")
    probe.add_argument("models", nargs="+", help="model ids as the provider names them")

    et = sub.add_parser("engine-test",
                        help="run a CLI tool worker (codex, cursor) in a throw-away git repo")
    et.add_argument("engine", help="engine name from config/engines.yaml")
    et.add_argument("--write", action="store_true",
                    help="ask it to create a file, then verify with git ourselves")

    bench = sub.add_parser("bench", help="benchmark Ollama models on this PC")
    bench.add_argument("models", nargs="*", help="model names (default: all pulled)")
    bench.add_argument("--tokens", type=int, default=128)

    # ---- tasks (Phases 3-5) ----
    run = sub.add_parser("run", help="delegate a task to a worker (foreground by default)")
    run.add_argument("role", help="research, coder, uiux, qa, reviewer ...")
    run.add_argument("instructions")
    run.add_argument("--project", help="git repo to work on (in allowed_project_roots)")
    run.add_argument("--engine", help="crew | codex | cursor (default: the role's engine)")
    run.add_argument("--accept", action="append", default=[], metavar="CRITERION",
                     help="acceptance criterion (repeatable)")
    run.add_argument("--context", action="append", default=[], metavar="FILE",
                     help="context file relative to the project (repeatable)")
    run.add_argument("--background", action="store_true",
                     help="start a background worker and return the task id")
    run.add_argument("--difficulty", default="auto",
                     choices=["auto", "light", "standard", "heavy"],
                     help="model size / reasoning effort (default: picked from the brief)")
    al = sub.add_parser("allow", help="let workers work on a git project folder")
    al.add_argument("path")

    rt = sub.add_parser("run-task", help=argparse.SUPPRESS)  # internal: the worker process
    rt.add_argument("task_id")
    rt.add_argument("--root")

    tasks = sub.add_parser("tasks", help="list tasks from the ledger")
    tasks.add_argument("--status")
    tasks.add_argument("--limit", type=int, default=30)
    for name, helptext in (("task", "show one task"), ("diff", "git diff of a task"),
                           ("verify", "run the project's checks in the task worktree"),
                           ("cancel", "stop a queued/running task")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("task_id")
    rev = sub.add_parser("revise", help="send feedback; the worker tries again")
    rev.add_argument("task_id")
    rev.add_argument("feedback")
    mg = sub.add_parser("merge", help="merge a verified task branch into your branch")
    mg.add_argument("task_id")
    mg.add_argument("--yes", action="store_true", help="you approve this merge")
    mg.add_argument("--allow-unverified", action="store_true")
    cl = sub.add_parser("cleanup", help="remove a task's worktree (and branch if merged)")
    cl.add_argument("task_id")
    cl.add_argument("--delete-branch", action="store_true",
                    help="also delete an UNMERGED branch (throws its work away)")
    sub.add_parser("report", help="usage per role / engine / model")
    sub.add_parser("mcp", help="run the workforce MCP server (stdio, for Claude Code)")

    args = parser.parse_args(argv)
    handlers = {
        "run": lambda: _cmd_run(args),
        "run-task": lambda: _cmd_run_task(args.task_id, args.root),
        "tasks": lambda: _cmd_tasks(args.status, args.limit),
        "task": lambda: _task_op("details", args.task_id),
        "diff": lambda: _task_op("diff", args.task_id),
        "verify": lambda: _task_op("verify", args.task_id),
        "cancel": lambda: _task_op("cancel", args.task_id),
        "revise": lambda: _task_op("revise", args.task_id, args.feedback),
        "merge": lambda: _task_op("merge", args.task_id, user_confirmed=args.yes,
                                  allow_unverified=args.allow_unverified),
        "cleanup": lambda: _task_op("cleanup", args.task_id,
                                    delete_unmerged_branch=args.delete_branch),
        "report": _cmd_report,
        "allow": lambda: _task_op("allow_project", args.path),
        "mcp": _cmd_mcp,
        "doctor": lambda: _cmd_doctor(as_json=args.json, live=not args.quick),
        "config": _cmd_config,
        "models": _cmd_models,
        "smoke": lambda: _cmd_smoke(args),
        "bench": lambda: _cmd_bench(args.models, args.tokens),
        "probe": lambda: _cmd_probe(args.provider, args.models),
        "engine-test": lambda: _cmd_engine_test(args.engine, write=args.write),
    }
    return handlers[args.command]()


def _load_settings():
    from workforce.settings import ConfigError, Settings

    try:
        return Settings.load()
    except ConfigError as exc:
        print("Config invalid:\n  " + "\n  ".join(exc.errors))
        return None


def _workforce(root: str | None = None):
    from pathlib import Path

    from workforce.settings import ConfigError, Settings
    from workforce.tasks import Workforce

    try:
        return Workforce(Settings.load(Path(root)) if root else Settings.load())
    except ConfigError as exc:
        print("Config invalid:\n  " + "\n  ".join(exc.errors))
        return None


def _print(obj) -> None:
    if isinstance(obj, str):
        print(obj)
    else:
        print(json.dumps(obj, indent=2, default=str, ensure_ascii=False))


def _cmd_run(args) -> int:
    from workforce.tasks import TaskError

    wf = _workforce()
    if wf is None:
        return 1
    try:
        t = wf.delegate(args.role, args.instructions, project_path=args.project,
                        engine=args.engine, acceptance=args.accept,
                        context_files=args.context, difficulty=args.difficulty,
                        start=args.background)
    except TaskError as exc:
        print(f"REFUSED: {exc}")
        return 1
    print(f"task {t.id}: {t.role} via {t.engine} (write={t.write}, difficulty={t.tier})"
          f"\nwhy: {t.tier_reason}"
          + (f"\nworktree: {t.worktree}" if t.worktree else ""))
    if args.background:
        print(f"running in the background: `workforce task {t.id}`")
        return 0
    if not wf.wait_for_slot(t.id):
        print("task was cancelled")
        return 1
    t = wf.execute(t.id)
    print(f"\n== {t.status} in {t.seconds}s, model: {t.model_used}")
    if t.error:
        print(f"error: {t.error}")
    print(f"\n{t.result or ''}")
    if t.verification:
        print(f"\nchecks: {t.verification['summary']}")
    print(f"\nsaved: {wf.runs_dir / t.id}")
    return 0 if t.status == "done" else 1


def _cmd_run_task(task_id: str, root: str | None) -> int:
    wf = _workforce(root)
    if wf is None:
        return 1
    if not wf.wait_for_slot(task_id):
        return 0  # cancelled while queued
    t = wf.execute(task_id)
    return 0 if t.status == "done" else 1


def _cmd_tasks(status: str | None, limit: int) -> int:
    wf = _workforce()
    if wf is None:
        return 1
    rows = wf.list(status, limit)
    if not rows:
        print("no tasks")
    for r in rows:
        print(f"{r['id']}  {r['status']:<9} {r['role']:<9} {r['engine']:<7} "
              f"r{r['revision']}  {r['title'][:60]}"
              + (f"  [{r['verified']}]" if r['verified'] else ""))
    return 0


def _task_op(op: str, *args, **kwargs) -> int:
    from workforce.tasks import TaskError

    wf = _workforce()
    if wf is None:
        return 1
    try:
        result = getattr(wf, op)(*args, **kwargs)
    except TaskError as exc:
        print(f"REFUSED: {exc}")
        return 1
    if hasattr(result, "status") and hasattr(result, "id"):
        result = {"task_id": result.id, "status": result.status, "revision": result.revision}
    _print(result)
    if op == "verify":
        return 0 if result.get("passed") else 1
    return 0


def _cmd_report() -> int:
    wf = _workforce()
    if wf is None:
        return 1
    rows = wf.ledger.stats()
    if not rows:
        print("no tasks yet")
        return 0
    print(f"{'role':<9} {'engine':<7} {'tier':<8} {'model':<36} {'tasks':>5} {'ok':>4} {'fail':>4} "
          f"{'merged':>6} {'avg s':>7} {'revs':>4}")
    for r in rows:
        print(f"{r['role']:<9} {r['engine']:<7} {r['tier']:<8} {r['model'][:36]:<36} "
              f"{r['tasks']:>5} {r['succeeded'] or 0:>4} {r['failed'] or 0:>4} "
              f"{r['merged'] or 0:>6} {r['avg_seconds'] or 0:>7} {r['revisions'] or 0:>4}")
    return 0


def _cmd_mcp() -> int:
    from workforce.mcp_server import main as serve

    serve()
    return 0


def _cmd_doctor(*, as_json: bool, live: bool) -> int:
    from workforce.doctor import FAIL, format_report, run_checks

    checks = run_checks(live=live)
    if as_json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        print(format_report(checks))
    return 1 if any(c.status == FAIL for c in checks) else 0


def _cmd_config() -> int:
    s = _load_settings()
    if s is None:
        return 1
    for name, agent in s.agents.agents.items():
        steps = []
        for logical in s.models.chains[agent.chain]:
            m = s.models.models[logical]
            steps.append(f"{logical}={m.provider}/{m.model or '?'}")
        print(f"{name:<9} {agent.role}")
        print(f"          chain '{agent.chain}': {' -> '.join(steps)}")
        print(f"          engine: {agent.engine}  tools: {', '.join(agent.tools) or '-'}  "
              f"write: {agent.allow_write}")
        for tier in ("light", "standard", "heavy"):
            c = s.models.chain_for(agent.chain, tier)
            models = " -> ".join(s.models.models[m].model or "?" for m in s.models.chains[c])
            effort = ""
            eng = s.engines.engines.get(agent.engine)
            if eng and eng.effort.get(tier):
                effort = f"   | {agent.engine} effort={eng.effort[tier]}"
            print(f"          {tier:<8} crew: {models}{effort}")
    return 0


def _cmd_models() -> int:
    from workforce.llm import providers
    from workforce.settings import secret_present

    s = _load_settings()
    if s is None:
        return 1
    configured = {(m.provider, m.model) for m in s.models.models.values()}
    for name, p in s.models.providers.items():
        print(f"== {name} ({p.type}){'' if p.enabled else '  [disabled]'}")
        if p.api_key_env and not secret_present(p.api_key_env):
            print(f"   {p.api_key_env} not set")
            continue
        key = os.environ[p.api_key_env].strip() if p.api_key_env else None
        try:
            found = providers.list_models(p, key)
        except providers.ProviderError as exc:
            print(f"   error: {exc}")
            continue
        for model_id in found:
            mark = "*" if (name, model_id) in configured else " "
            print(f" {mark} {model_id}")
    print("\n* = used in config/models.yaml")
    return 0


def _cmd_smoke(args) -> int:
    from workforce.llm.router import ChainExhaustedError, NoUsableModelError, build_llm

    s = _load_settings()
    if s is None:
        return 1
    if args.break_cloud:
        for p in s.models.providers.values():
            if p.api_key_env and not p.local:
                os.environ[p.api_key_env] = "invalid-key-for-fallback-test"
        print("(cloud keys replaced with an invalid key for this run only)")
    try:
        if args.chain:
            llm = build_llm(s, args.chain, local_only=args.local_only)
        else:
            llm = build_llm(s, models=[args.model], local_only=args.local_only)
    except (NoUsableModelError, KeyError) as exc:
        print(f"FAIL: {exc}")
        return 1

    started = time.monotonic()
    try:
        answer = llm.call(SMOKE_PROMPT)
    except ChainExhaustedError as exc:
        print(f"FAIL: {exc}")
        for f in exc.failures:
            print(f"   {f.model}: {f.kind}: {f.detail[:160]}")
        return 1
    elapsed = time.monotonic() - started
    for h in llm.history:
        print(f"   attempt {h['model']}: {h['outcome']} ({h['seconds']}s)")
    print(f"OK: answered by {llm.last_used} in {elapsed:.1f}s -> {str(answer).strip()[:80]!r}")
    return 0


def _cmd_probe(provider_name: str, models: list[str]) -> int:
    from workforce.llm import providers
    from workforce.settings import secret_present

    s = _load_settings()
    if s is None:
        return 1
    p = s.models.providers.get(provider_name)
    if p is None:
        print(f"unknown provider '{provider_name}' (see config/models.yaml)")
        return 1
    if p.api_key_env and not secret_present(p.api_key_env):
        print(f"{p.api_key_env} not set")
        return 1
    key = os.environ[p.api_key_env].strip() if p.api_key_env else None
    usable = 0
    for m in models:
        r = providers.probe(p, key, m)
        usable += r.ok
        status = "OK  " if r.ok else "FAIL"
        print(f"{status} {m:<28} {r.seconds:>6}s  {r.detail[:90]}")
    print(f"\n{usable}/{len(models)} usable")
    return 0 if usable else 1


def _cmd_engine_test(name: str, *, write: bool) -> int:
    import subprocess

    from workforce.engines import EngineUnavailable, get_engine
    from workforce.engines.registry import provider_secret_names

    s = _load_settings()
    if s is None:
        return 1
    try:
        engine = get_engine(s, name)
        print(f"binary: {engine.resolve_binary()}")
        if write and not engine.supports_write:
            raise EngineUnavailable(f"engine '{name}' is read-only on this OS (by design)")
    except EngineUnavailable as exc:
        print(f"REFUSED: {exc}")
        return 1

    # Throw-away repo under workspace/ (git-ignored).
    repo = s.resolve(s.policies.paths.workspace_dir) / "_selftest" / f"{name}-{int(time.time())}"
    repo.mkdir(parents=True)
    (repo / "README.md").write_text("# selftest\n", encoding="utf-8")
    for git_args in (["init", "-q"], ["add", "."],
                     ["-c", "user.name=workforce", "-c", "user.email=workforce@localhost",
                      "commit", "-q", "-m", "init"]):
        subprocess.run(["git", *git_args], cwd=repo, check=True, capture_output=True)

    if write:
        expected = f"hello from {name}"
        prompt = (f"Create a file named hello.txt in the current directory containing exactly "
                  f"the text: {expected}\nDo not modify or create any other file. "
                  f"Then reply with: DONE")
    else:
        prompt = "Reply with exactly the single word: PONG"
    print(f"repo: {repo}\nrunning {name} ({'write' if write else 'read-only'})...")
    result = engine.run(prompt, repo, write=write, secret_names=provider_secret_names(s))
    print(f"exit={result.exit_code} timed_out={result.timed_out} {result.seconds}s")
    print(f"tool says: {result.final_message[:200]!r}")
    if not result.ok:
        print("--- log tail ---\n" + result.log_tail[-1200:])
        return 1
    if not write:
        return 0

    # Independent verification: trust git, not the tool's claim.
    status = subprocess.run(["git", "status", "--porcelain", "--untracked-files=all"],
                            cwd=repo, capture_output=True, text=True).stdout.split("\n")
    changed = sorted(line[3:] for line in status if line.strip())
    hello = repo / "hello.txt"
    content = hello.read_text(encoding="utf-8").strip() if hello.is_file() else None
    print(f"git sees changed files: {changed}")
    print(f"hello.txt content: {content!r}")
    passed = changed == ["hello.txt"] and content == expected
    print("VERIFIED: exactly one new file with the right content" if passed
          else "NOT VERIFIED: result does not match the request")
    return 0 if passed else 1


def _cmd_bench(models: list[str], tokens: int) -> int:
    from workforce.llm import providers

    s = _load_settings()
    if s is None:
        return 1
    url = s.models.providers["ollama"].resolved_base_url()
    try:
        models = models or providers.ollama_list_models(url)
    except providers.ProviderError as exc:
        print(f"Ollama: {exc}")
        return 1
    if not models:
        print("no Ollama models pulled yet (ollama pull <model>)")
        return 1
    print(f"{'model':<28} {'load s':>7} {'prompt t/s':>11} {'gen t/s':>8} {'total s':>8}")
    for m in models:
        try:
            r = providers.ollama_benchmark(url, m, max_tokens=tokens)
        except providers.ProviderError as exc:
            print(f"{m:<28} error: {exc}")
            continue
        print(f"{m:<28} {r.load_seconds:>7} {r.prompt_tokens_per_second:>11} "
              f"{r.gen_tokens_per_second:>8} {r.total_seconds:>8}")
    return 0
