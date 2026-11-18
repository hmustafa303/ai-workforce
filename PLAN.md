# AI Development Workforce: Architecture & Setup Plan

> **Status:** Phases 1, 2, 2b, 3, 4, 5 ✅ · Phase 8 partly ✅ · Phases 6–7: workforce side ready, waiting for a real UI task.
> **Next:** use it on your own project (add it to `allowed_project_roots`). See "Phase 3–5 results" at the end.
> **Date:** 2026-11-18
> **Location:** `C:\Users\Hammad\ai-workforce`

**Short version:** Claude Code (the one in VS Code, your subscription) is the **Parent**: it plans, delegates, checks the work and approves it.
The **Child** workers run in CrewAI on free models (Gemini free tier and Ollama on your PC).
Claude never trusts a worker's own claims. It reads the actual git diff and runs the tests itself before approving anything.

---

## 1. Phase 0: Environment Analysis (done)

| Area | Finding | Impact |
|---|---|---|
| CPU | AMD Ryzen 5 7430U, 6 cores / 12 threads | OK for orchestration; local LLMs run on the CPU only |
| RAM | 15.4 GB total, ~3.3 GB free at the time of the scan | Local models limited to ~3B–7B (quantized). Close heavy apps when running 7B |
| GPU | AMD Radeon integrated (512 MB) | No useful GPU acceleration for Ollama → **CPU inference** |
| Disk | C: 86 GB free | Enough for Ollama + 2–3 models (~2–5 GB each) |
| OS security | **Smart App Control = ON (enforce)**; it already blocks some `.exe` shims | Installers must be signed (Ollama, Node, uv are). Unsigned tools may be blocked |
| Python | Only 3.15.2 (system + uv-managed) | **CrewAI 1.15.x requires Python `>=3.10,<3.14`** → project will use a uv-managed **Python 3.12** venv. System Python stays untouched |
| uv | 0.13.6 ✅ | Used for Python version + dependency management |
| Node / npm | v26.13 / 11.20 ✅ | Needed for Playwright MCP |
| Git | 2.56, user name/email set ✅ | Used for sandboxing worker changes (branches/worktrees) |
| Claude Code | 2.1.325 ✅ (VS Code extension + CLI) | **The Parent AI.** No Anthropic API key is needed |
| MCP (already configured) | `figma`, `gmail` (user-level) | Figma MCP is reused for the UI/UX flow |
| Ollama | ❌ not installed | Install in Phase 2 |
| GitHub CLI (`gh`) | ❌ not installed | Optional (Phase 8) |
| Docker / WSL | ❌ / WSL broken (`REGDB_E_CLASSNOTREG`) | **Not needed.** The design avoids both |
| Gemini | `~/.gemini` OAuth login exists (Code Assist/Antigravity) but **no API key** | CrewAI needs a free **Google AI Studio API key** (Phase 2) |
| Codex / ChatGPT | `~/.codex` auth exists | Optional extra worker via the Codex CLI (Phase 8) |
| API keys in environment | None (besides Claude's own internal token) | Clean start; keys will live in a git-ignored `.env` |

---

## 2. Key Architecture Decisions

### Decision 1: Claude Code is the parent, via a custom MCP server
CrewAI agents normally need an LLM API key for the "manager". **You use Claude through your subscription, not the API**, so Claude cannot sit *inside* CrewAI for free.

**Solution:** turn it around. We build a small **`workforce` MCP server** (Python) that exposes the CrewAI workers as *tools* Claude Code can call.
Claude stays in VS Code as the brain and calls `delegate_task(...)`, `get_task(...)` and `revise_task(...)`.
CrewAI handles the worker side: roles, prompts, tools, model routing and retries.

```
                ┌──────────────────────────────────────────────┐
   YOU ───────▶ │  CLAUDE CODE (Parent / Manager) in VS Code    │
                │  plans · delegates · reviews · verifies       │
                └───┬──────────────┬──────────────┬────────────┘
                    │ MCP          │ MCP          │ built-in tools
                    ▼              ▼              ▼
     ┌──────────────────┐  ┌─────────────┐  ┌─────────────────────────┐
     │ workforce MCP     │  │ Figma MCP   │  │ Bash/Git/Read/Edit       │
     │ (our Python code) │  │ Playwright  │  │ pytest · npm test · lint │
     └────────┬─────────┘  │ MCP         │  │ ← INDEPENDENT VERIFY     │
              │            └─────────────┘  └─────────────────────────┘
              ▼
   ┌───────────────────── CrewAI Worker Layer ─────────────────────┐
   │  Research   │  Coder      │  UI/UX       │  QA / Tester        │
   │  agent      │  agent      │  agent       │  agent              │
   └──────┬──────┴──────┬──────┴──────┬───────┴──────┬─────────────┘
          ▼             ▼             ▼              ▼
   ┌──────────────── LLM Router (fallback chain) ─────────────────┐
   │  Gemini (free tier) → Groq/OpenRouter free (optional) → Ollama │
   └───────────────────────────────────────────────────────────────┘
          │
          ▼
   Task ledger (SQLite)  +  Git worktree sandbox per task  +  run logs
```

### Decision 2: workers never write to your real code directly
Each coding task gets its **own git branch and worktree** (`workspace/<task-id>/`).
Workers write there only. Claude inspects the diff, runs the tests in that worktree, and merges only after the checks pass **and you say OK**.

### Decision 3: verification is deterministic, not "the AI says it works"
"Done" is defined by commands that either pass or fail: tests, lint, type-check, Playwright, build.
A worker's self-report is treated as a *claim*. Claude re-runs every check itself with its own tools.

### Decision 4: everything is config-driven
Models, agents and limits live in YAML (`config/`). You can add a new worker or swap a model without changing code.
This is what makes the workforce expandable.

### Decision 5: free and local first, with honest trade-offs
| Worker | Default model | Fallback | Why |
|---|---|---|---|
| Research | Gemini Flash-class (free tier) + free web search (DuckDuckGo) | Ollama small model | Needs speed + large context |
| Coder | Gemini (best free-tier coding model available on your key) | Ollama coder model (e.g. `qwen2.5-coder` 7B or newer equivalent) | Your CPU makes local 7B slow (~5–8 tokens/sec) |
| UI/UX | Gemini (multimodal: can read screenshots) | Ollama | Design context comes from Figma via Claude |
| QA | Ollama small model for drafting test cases | Gemini | Real execution is pytest/Playwright, not the LLM |

Exact model IDs are **not hard-coded now**. In Phase 2 we list the models your key can actually use and benchmark the local ones on your PC, then fill in `config/models.yaml`.

> **Update (Phase 2, 2026-11-18):** the Gemini *API* free tier is **not available**
> for your Google Cloud project: HTTP 403, and Google says the only fix is billing. Gemini is
> therefore used through the **Gemini CLI with your Google login** (Decision 6).
> **Groq** (free key) replaced it as the cloud API model. **OpenRouter was dropped** at your request.

### Decision 6: Two kinds of Child workers (added in Phase 2)
You want the children to be **free AI tools** (Gemini, Cursor, ChatGPT/Codex, ...), not only raw models:

| Kind | What it is | Examples | Good for |
|---|---|---|---|
| **A. CrewAI model workers** | A CrewAI agent (role, prompt, our own tools) that thinks with an API or local model, through the fallback router | Groq (`gpt-oss-120b`, `qwen3.8-27b`), Ollama (local) | research, test plans, reviews, small code |
| **B. CLI tool workers** | A complete AI coding tool run headless, inside the task's git worktree, with its own login | **Gemini CLI** (Google login and student plan), **Codex CLI** (ChatGPT login), **Cursor CLI** (Cursor account) | real multi-file coding and UI work |

Claude (the parent) manages both kinds the same way: `delegate_task(role, engine=...)` → worktree → **independent verification** → `revise_task`.
CLI tools never touch your real branch. They run only inside the task worktree, and Claude checks their diff like any other worker's.

---

## 3. Security Plan (API keys and access)

1. **Secrets only in `.env`** (git-ignored). `.env.example` is committed with blank values.
2. **Claude Code is denied from reading `.env`**, using `permissions.deny` in `.claude/settings.json`. Keys are loaded by the Python process only.
3. **Workers have scoped tools:**
   - read-only access to the target project (an allow-listed path)
   - write access **only** inside their own worktree
   - **no shell, no network** except the research agent's web search
4. **No secrets are sent to free-tier models.** ⚠️ The Gemini *free tier* may use prompts to improve Google products. Do not delegate proprietary or secret code to free-tier workers. Mark sensitive projects `local_only: true` in config, which forces Ollama.
5. **Approval gates:** Claude Code's permission prompts stay on. Merging into your main branch always needs your confirmation.
6. Logs are stored with keys masked.

---

## 4. Project Structure (created in Phase 1)

```
ai-workforce/
├── PLAN.md                    ← this file
├── README.md                  ← how to run
├── CLAUDE.md                  ← Parent-AI operating rules (delegate → verify → approve)
├── pyproject.toml             ← deps (crewai, mcp, litellm, playwright, ...)
├── .python-version            ← 3.12
├── .env.example / .env        ← keys (.env git-ignored)
├── .gitignore
├── .mcp.json                  ← project MCP servers: workforce, playwright
├── .claude/settings.json      ← permissions (deny .env, allow safe commands)
├── config/
│   ├── models.yaml            ← providers + fallback chains + rate limits
│   ├── agents.yaml            ← role, goal, backstory, model, tools per worker
│   └── policies.yaml          ← max retries, timeouts, allowed paths, local_only
├── src/workforce/
│   ├── cli.py                 ← `workforce doctor | run | status | tasks`
│   ├── settings.py            ← loads .env + YAML, validates
│   ├── llm/router.py          ← builds the LLM with fallback + retry/backoff
│   ├── agents/                ← research.py, coder.py, uiux.py, qa.py
│   ├── tools/                 ← fs_read.py, worktree_write.py, web_search.py
│   ├── ledger.py              ← SQLite task ledger (status, attempts, results)
│   ├── sandbox.py             ← git worktree create/diff/cleanup
│   ├── verify.py              ← runs tests/lint/build, returns structured results
│   └── mcp_server.py          ← MCP server exposing tools to Claude
├── tests/                     ← tests for the workforce itself
├── workspace/                 ← per-task worktrees (git-ignored)
└── runs/                      ← outputs + logs per task (git-ignored)
```

### MCP tools Claude will get
| Tool | Purpose |
|---|---|
| `workforce_doctor()` | Health of every provider/model (is Ollama up? is the Gemini key valid? rate-limit state) |
| `delegate_task(role, instructions, project_path, context_files, acceptance_criteria)` | Starts a worker task in the background → returns `task_id` |
| `get_task(task_id)` | Status, worker output, worktree path, diff summary, worker-run check results |
| `revise_task(task_id, feedback)` | Sends Claude's corrections back to the same worker (bounded retries) |
| `list_tasks(status?)` | Ledger view |
| `cancel_task(task_id)` / `cleanup_task(task_id)` | Housekeeping |

Tasks run **asynchronously** (local CPU models can take minutes). Claude starts a task and checks it with `get_task`, so MCP calls don't time out.

### The Parent loop (written into `CLAUDE.md`)
```
1. Understand the requirement → write a plan with acceptance criteria
2. Split it into tasks → pick a worker per task (role + model)
3. delegate_task → wait → get_task
4. INDEPENDENT VERIFY: git diff in the worktree, run tests/lint/build, Playwright for UI
5. Fail → revise_task with exact errors (max N rounds) → else Claude fixes it or escalates to you
6. Pass → summarize → ask you → merge → cleanup
```

---

## 5. Phased Implementation (each phase is tested before moving on)

### Phase 1: Foundation
- Create the folder, `git init`, `.gitignore`, `.env.example`, `pyproject.toml`
- `uv python install 3.12` → project venv → install `crewai` and `mcp`
- Config skeletons (`models.yaml`, `agents.yaml`, `policies.yaml`) + a settings loader with validation
- `.claude/settings.json` (deny `.env`) + first `CLAUDE.md`
- **Test / done when:** `uv run workforce doctor` runs and reports the config as valid. `import crewai` works. Unit tests for the settings loader pass.

**✅ Phase 1 result (2026-11-18)**
- **Change from the plan:** the uv-downloaded Python and its `.venv` launcher are
  *unsigned*, and Smart App Control blocked them. The project now uses the
  **signed python.org Python 3.13.17** (installed for the user only with winget, not on PATH;
  CrewAI supports `<3.14`). The `.venv` launcher is PSF-signed and allowed.
- Unsigned script launchers (`pytest.exe`) are still blocked, so always use `uv run python -m <tool>`.
- `workforce doctor`: 6 ok, 4 expected warnings (models not chosen, no Gemini key,
  Ollama not installed, no project roots yet), 0 failures. CrewAI 1.15.41, MCP 1.30.0.
- `pytest`: 17 passed. `ruff`: clean.
- CrewAI telemetry is disabled by default (`CREWAI_DISABLE_TELEMETRY`, `OTEL_SDK_DISABLED`).

### Phase 2: Model providers
- **You:** create a free Gemini API key at Google AI Studio and paste it into `.env` (I never see it)
- Install **Ollama** (signed installer, should pass Smart App Control), then pull 1 small general model + 1 coder model
- Build `llm/router.py`: fallback chain, retry with backoff on HTTP 429 rate limits, timeout, `local_only` enforcement
- Benchmark local models on your CPU (tokens/sec) → pick the defaults
- Optional: free Groq / OpenRouter keys as extra fallbacks
- **Test / done when:** `workforce doctor` shows Gemini ✅ Ollama ✅. A smoke prompt succeeds through each provider. Forcing a Gemini failure falls back to Ollama.

**Phase 2 result so far (2026-11-18)**
- **Gemini API:** key valid, but the project is **not eligible for the free tier** (403 on every model) → the provider is disabled. Gemini comes in via the CLI (Phase 2b).
- **Groq:** ✅ `cloud_fast = qwen/qwen3.8-27b` (~0.9s), `cloud_code = openai/gpt-oss-120b`.
  (Fixed along the way: Cloudflare in front of Groq was blocking Python's default User-Agent.)
- **Ollama 0.40.16:** ✅ signed installer. CPU benchmark (generation tokens/s): `qwen2.5-coder:3b` 17.0, `llama3.2:3b` 15.7, `qwen3.5:4b` 9.4.
  → `local_general = llama3.2:3b`, `local_coder = qwen2.5-coder:3b`. `gemma4:e4b` is still downloading (it will be benchmarked as an extra).
- **✅ Phase 2 done-criteria met:** doctor 13 ok / 0 fail, every model verified, smoke tests pass for the cloud and local paths, fallback proven.
- **Router:** `FallbackLLM` retries rate limits with backoff and skips straight to the next model on auth, not-found or unreachable errors.
  Tested live: **a broken Groq key fell back to Ollama automatically** (`smoke --break-cloud`).
- **New commands:** `models`, `probe <provider> <ids>`, `smoke --chain/--model [--local-only] [--break-cloud]`, `bench`.
  `doctor` now sends a live test prompt to each cloud provider.
- **Tests:** 54 passed, all offline (fake LLMs and providers, frozen test config in `tests/fixtures/config`).

### Phase 2b: CLI tool workers (pulled forward from Phase 8)
- Install and log in: **Gemini CLI** (npm, Google login), **Codex CLI** (signed `codex.exe` already on your PC from the ChatGPT extension; ChatGPT login), **Cursor CLI** (needs a Cursor install and account; last, only if its Windows CLI and free plan allow it)
- `engines/cli_worker.py`: run a tool **headless** with a prompt, **cwd = task worktree**, with a timeout; capture its output and the git diff
- Each tool runs with its safest non-interactive settings (no auto-approval of shell commands outside the worktree)
- **Test / done when:** each installed tool answers a headless prompt, and makes a small change in a throw-away worktree that `git diff` shows.

**Phase 2b progress (2026-11-18)**
- ❌ **Gemini CLI / Antigravity:** the free personal tier is discontinued, and Google's terms forbid driving it from Claude Code (account suspension risk). It was uninstalled.
- ✅ **Codex** (signed `codex.exe` from the ChatGPT extension, ChatGPT login): `engine-test codex` → PONG.
  `engine-test codex --write` → git confirms exactly `hello.txt` with the right content (VERIFIED).
  It runs with `--sandbox read-only|workspace-write`, `--ephemeral`, the prompt on stdin, provider keys stripped from its environment, and a process-tree kill on timeout.
- ✅ **Cursor CLI** (we read the installer script before running it; the signed `node.exe` is called directly, not via `cmd.exe`):
  read-only `engine-test cursor` → PONG. **Live guard-rail tests:**
  - shell blocked by the deny rules ✅
  - but on Windows its file writes could **not** be confined to the task folder (`--force` → wrote outside;
    no `--force` → still wrote outside; deny absolute paths → blocked inside writes too; `--sandbox` → macOS/Linux only)

  → **Cursor is READ-ONLY** (plan, review, analysis). The adapter refuses write mode.
- **Codex guard-rail test:** writing outside its folder was **blocked** ✅. Shell commands run *inside* its
  workspace-write sandbox, which is by design: it needs them to run tests.
- Tests: 69 passed and ruff is clean.

---

## Revised next steps (after Phase 2b)

Goal: use the workforce on **your existing project** as soon as possible, still step by step and tested.

| Step | Phase | Delivers | You need to give |
|---|---|---|---|
| 1 | **3** | Task ledger (SQLite) + `workforce run research "..."` (Groq/Ollama CrewAI agent with web search) | nothing |
| 2 | **4** | `workforce` MCP server registered for Claude Code at **user level**: `delegate_task`, `get_task`, `revise_task`, `list_tasks` | a VS Code reload |
| 3 | **5** | git worktree per task in your project, Codex writes, Cursor reviews, verification (tests/lint/build), revise loop, merge **only after your yes** | your project's path, and committed work |
| 4 | 6–8 | QA + Playwright, UI/UX + Figma, hardening | as needed |

### Phase 3: First worker end-to-end (Research)
- Research agent + free web search tool + task ledger + `runs/` output
- CLI: `workforce run research "compare X vs Y"`
- **Test / done when:** a research task produces a sourced report, and the ledger records status, model used, duration and attempts.

### Phase 4: Connect Claude (the MCP server)
- `mcp_server.py` with the tools above, registered in `.mcp.json`
- **Test / done when:** from VS Code chat, Claude calls `workforce_doctor` and `delegate_task(role="research")`, reads the result with `get_task`, and evaluates it.

### Phase 5: Coder worker + sandbox + verification loop
- Git worktree per task; worker tools: read the project, write only inside the worktree
- `verify.py`: auto-detects the project type (Python → pytest/ruff; Node → npm test/eslint/build)
- `revise_task` loop with a retry limit
- **Test / done when:** on a small sample repo, Claude delegates a feature, a test fails, Claude sends feedback, the worker fixes it, Claude re-verifies independently, and the merge happens only after your OK.

### Phase 6: QA worker + Playwright
- Install Playwright (Python package + Chromium) and add the **Playwright MCP** (pinned version) to `.mcp.json`
- QA agent drafts test plans/cases. Tests are executed by real tooling
- **Test / done when:** QA writes e2e tests for a sample page, Playwright runs them, and Claude also checks the page itself via Playwright MCP (screenshots, console errors).

### Phase 7: UI/UX worker + Figma
- Flow: Claude reads the Figma frame (existing Figma MCP: design context + screenshot) → passes a spec to the UI/UX worker → the worker builds components in the worktree → Playwright screenshot → Claude compares with the Figma screenshot
- The worker never gets Figma credentials; Claude passes on only what's needed
- **Test / done when:** one Figma frame becomes a working component that passes the visual + functional checks.

### Phase 8: Hardening and expansion
- Structured logging, per-provider usage/rate counters, a `workforce report` command
- Claude Code slash command / skill: `/delegate` and `/verify` for a one-step workflow
- (CLI workers moved to Phase 2b)
- Optional: GitHub CLI (`gh`) for PRs; Context7/docs MCP for up-to-date library docs
- **Test / done when:** a full multi-worker feature (research → code → QA) is delivered on a real project of yours.

---

## 6. Honest Limitations
- **Local models will be slow** on this laptop (CPU only). They're good for small, cheap jobs; heavy coding goes to Gemini.
- **Free tiers have rate limits** that change over time. The router handles HTTP 429 errors with backoff and fallback, but large jobs may queue.
- **Your Claude subscription limits still apply**, because Claude does the planning and verification. Delegating the bulk work to the free workers helps save Claude usage.
- **Gemini student plan:** it mainly raises limits in the Gemini app, Gemini CLI and Code Assist, so the Gemini CLI worker (Phase 2b) is the way to use it.
  The Gemini *API* is not free for your current project.
- **CLI tools' free plans** (Gemini CLI, Codex with a free ChatGPT account, Cursor Hobby) have daily or monthly limits that the vendors change.
  When one runs out, Claude switches to another engine or to the Groq/Ollama workers.

---

## 7. What I Need From You Before / During Phase 1–2
1. ✅/❌ Approve this architecture and the location `C:\Users\Hammad\ai-workforce`
2. In Phase 2: a Gemini API key from https://aistudio.google.com (free). You paste it into `.env` yourself
3. In Phase 2: OK to install Ollama (~1 GB) + models (~2–5 GB each)
4. In Phase 5: a sample/real project to test the coder loop on (or I'll create a small demo repo)

---

## Phase 3–5 results (2026-11-18)

**Built:** `ledger.py` (SQLite tasks/events/feedback), `sandbox.py` (worktree per task, path safety,
commit, diff, merge, cleanup), `verify.py` (`.workforce.yaml` or auto-detected pytest/ruff/npm checks),
`tools/` (DuckDuckGo search, file list/read/write scoped to the worktree), `tasks.py` (delegate → detached
worker process → commit → auto-verify → revise → merge → cleanup), `mcp_server.py` (11 tools + 2 prompts),
new CLI commands, `doctor` checks for engines and the ledger. Default engine per role in `agents.yaml`,
new `reviewer` role (Cursor). 99 offline tests, ruff clean.

**Live tests on this PC:**
| Test | Result |
|---|---|
| Phase 3: `run research` (CrewAI + Groq + web search) | ✅ sourced report; ledger shows status, model, duration, attempts |
| Phase 4: MCP over real stdio | ✅ initialize, 11 tools, 2 prompts, doctor/list/get calls; `claude mcp get workforce` → Connected (user scope) |
| Phase 5: Codex coder task on a demo repo, in a background process | ✅ worktree + branch, committed, auto-verify PASSED (pytest + hidden acceptance check) in 62 s |
| Independent check by the Parent | ✅ read the real diff, ran pytest and the acceptance cases myself |
| Revise loop | ✅ feedback round 1 → Codex continued in the same worktree (r1 commit) → 6 tests pass, re-verified by hand |
| Merge gate | ✅ `merge` without the user's yes → REFUSED; demo `main` untouched |
| Reviewer (Cursor, read-only) via the task system | ✅ concrete review; git confirms zero changes; cleanup removed worktree + empty branch |

**Problems found and fixed along the way:**
- **MCP server hang on Windows:** a child process (git, or one a library starts) that inherits the
  server's stdin pipe blocks in CreateProcess while the server is reading it. Fix: the server moves the
  protocol pipe to a private fd and points standard input at NUL; all our subprocesses also use `stdin=DEVNULL`.
- **Groq `tool_use_failed` (HTTP 400):** a one-off malformed tool call sent the run to slow local Ollama
  (first research run took 408 s). The router now retries the same model once for that case.
- **Cleanup overwrote the status** (merged history was lost) → now a separate `cleaned_at` field.
- **`__pycache__` counted as uncommitted work** and would have blocked merges → caches/venvs are ignored.
- **Research quality when Groq is rate-limited:** the 3B local fallback produced a wrong fact and an
  invented source. Added deterministic red flags in `get_task`: `source_check` (cited URLs that no
  web_search returned) and `quality_warning` (answer came from a fallback model).

**Known limits:**
- Groq's free tier rate-limits quickly under heavy use; then research falls back to the weak local model.
- Each task starts from your last commit; uncommitted work is invisible to workers (a warning is logged).
- A `reviewer` task reviews your branch, not another task's branch.
- Verification runs the project's own test code on your PC (with secrets stripped from its environment).
