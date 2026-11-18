# AI Workforce

**Claude Code is the Parent.** It plans the work, delegates tasks, checks the results and approves them.
**Free AI tools and models are the Children:** Codex, Cursor, Groq and Ollama.
Claude never trusts a child's own claims. It checks the real `git diff` and runs the tests itself.

See [PLAN.md](PLAN.md) for the architecture and phases, and [CLAUDE.md](CLAUDE.md) for the parent's rules.

---

## Current status (2026-11-18)

| Phase | What | Status |
|---|---|---|
| 1 | Foundation: config, settings, doctor, tests | ✅ done |
| 2 | Model providers + fallback router | ✅ done |
| 2b | CLI tool workers (Codex, Cursor) | ✅ done |
| 3 | Task ledger + Research worker (CrewAI + web search) | ✅ done |
| 4 | `workforce` MCP server, registered for Claude Code at user level | ✅ done |
| 5 | Git worktree per task + verification + revise loop + approved merge | ✅ done (tested on a demo repo) |
| 6–7 | Playwright e2e, Figma → UI | 🟡 the workforce side is ready (see below); needs your first real UI task |
| 8 | Hardening: `report`, `/delegate` and `/verify` prompts | ✅ partly done |

**It's ready for your own projects.** Add the project's folder to `allowed_project_roots` (step 2 below).

### The child workers

| Role | Default worker | Can write code? | Use it for |
|---|---|---|---|
| `coder` | **Codex** (your ChatGPT login) | ✅ only inside its own task worktree | features, fixes, refactors |
| `uiux` | **Codex** | ✅ same | UI components from a spec or Figma frame |
| `qa` | **Codex** | ✅ same | unit and Playwright tests |
| `reviewer` | **Cursor** (your Cursor login) | ❌ read-only | code review, plans, second opinions |
| `research` | **CrewAI** on Groq → Ollama fallback, with DuckDuckGo search | ❌ | sourced research reports |

Any role can be switched to another engine for one task (`engine="crew"`, `"codex"` or `"cursor"`).
For a project marked local-only, only `crew` is allowed, and it then runs on Ollama only.

**Not used, on purpose:** Gemini CLI / Antigravity on your Google login (Google's terms forbid it),
the Gemini API key (not eligible for the free tier), OpenRouter (removed at your request).

---

## Using it with your own project

1. **Commit your work.** Workers start from your last commit and never see uncommitted changes.
2. **Allow the workforce to work on it.** In `config/policies.yaml`:
   ```yaml
   paths:
     allowed_project_roots:
       - C:/path/to/your-project      # or a parent folder such as C:/Users/Hammad/Projects
   ```
   Private code that must never reach a cloud model: also list it under `privacy.local_only_projects`.
3. **Optional, recommended: tell it how to check your project.** Put `.workforce.yaml` in the project root:
   ```yaml
   setup:                       # optional, runs in the worktree before the checks
     - npm ci
   checks:                      # every command must exit 0
     - {name: lint,  cmd: npm run lint}
     - {name: tests, cmd: npm test}
     - {name: build, cmd: npm run build}
   ```
   Without it, checks are auto-detected: Python → pytest (+ ruff if configured);
   Node → the npm scripts `lint`, `typecheck`, `test`, `test:e2e`/`e2e`, `build`.
   The checks are always read from **your** checkout, never from the worker's copy, so a worker can't weaken them.
4. **Open your project in VS Code and ask Claude normally**, e.g.
   *"Add a contact form with validation. Let the workforce do it."*, or use the slash command
   **`/mcp__workforce__delegate`**. What happens:
   ```
   Claude: requirement + acceptance criteria
     → delegate_task(role="coder")        Codex works in workspace/<task-id> on branch workforce/<task-id>
     → get_task                           status, changed files, files to "review closely", auto-check results
     → task_diff + verify_task + its own commands   ← independent verification
     → revise_task with the exact errors  (max 3 rounds, then Claude fixes it or asks you)
     → summary + evidence → asks YOU → merge_task(user_confirmed=true) → cleanup_task
   ```
5. **You stay in control.** `merge_task` refuses unless the user confirmed it, the last verification
   passed, and it ran on the exact code being merged. Your checkout must be clean and on the same branch.

### Automatic model switching (difficulty tiers)
Every task gets a difficulty: `light`, `standard` or `heavy`. By default it's picked from the
brief by simple rules, with no LLM call and no tokens spent: "typo/rename/docstring/summary" → light;
"auth/payment/refactor/migration/concurrency/security/performance", long briefs, many files → heavy.
Claude can also set it (`difficulty=`). Each tier picks its own models:

| Tier | Codex reasoning effort | CrewAI chain (research) | CrewAI chain (coding) |
|---|---|---|---|
| light | `low` | gpt-oss-20b → qwen3.8-27b → llama3.2:3b | qwen3.8-27b → gpt-oss-20b → qwen2.5-coder:3b |
| standard | `medium` | qwen3.8-27b → gpt-oss-20b → llama3.2:3b | gpt-oss-120b → qwen3.8-27b → qwen2.5-coder:3b |
| heavy | `high` | gpt-oss-120b → qwen3.8-27b → llama3.2:3b | gpt-oss-120b → qwen3.8-27b → qwen2.5-coder:3b |

- **Escalation:** every `revise_task` round moves the task one tier up (`escalate_on_revise` in policies).
- **Rate limits:** Groq limits are per model, so each chain tries a second Groq model before the slow local one.
- Change everything in `config/models.yaml` (`<chain>@<tier>`) and `config/engines.yaml` (`effort`, `tier_models`).

### Using it from the Claude terminal, in any project
The MCP server is registered at user level, and `~/.claude/CLAUDE.md` tells Claude, in every project,
to delegate the bulk work and only plan and verify itself. That's where the token saving comes from.
```powershell
cd C:\path\to\any-project
claude                       # then just ask, e.g. "add pagination to the users page"
```
If the project isn't allowed yet, Claude asks you and then calls `allow_project`
(or run `uv run python -m workforce allow C:\path\to\project` yourself).

**What it saves, honestly:** Claude still spends tokens on planning, reading `get_task`/diffs and
checking. It saves the big part: writing and rewriting the code, tests and research. Very small
edits are cheaper for Claude to do directly than to delegate and review.

### MCP tools Claude gets (in every project)
`workforce_doctor`, `list_roles`, `delegate_task`, `get_task`, `task_diff`, `verify_task`,
`revise_task`, `list_tasks`, `cancel_task`, `merge_task`, `cleanup_task`, `allow_project`.
Prompts: `/mcp__workforce__delegate`, `/mcp__workforce__verify`.
The Parent's operating rules ride along in the server's instructions, so they apply in every project.

---

## Commands
```powershell
uv run python -m workforce doctor                  # health + safety (add --quick to skip live prompts)
uv run python -m workforce config                  # roles -> engine, chain, models
uv run python -m workforce run research "compare X vs Y"           # research, in the foreground
uv run python -m workforce run coder "add ..." --project C:\path\to\repo --accept "npm test passes" --background
uv run python -m workforce tasks                   # the ledger
uv run python -m workforce task <id>               # status, changed files, checks, events
uv run python -m workforce diff <id>               # the real git diff
uv run python -m workforce verify <id>             # run the project's checks in the worktree
uv run python -m workforce revise <id> "exact errors / what to change"
uv run python -m workforce merge <id> --yes        # only after YOU decided to merge
uv run python -m workforce cleanup <id>            # remove worktree (+ branch if merged or empty)
uv run python -m workforce cancel <id>
uv run python -m workforce report                  # usage per role / engine / tier / model
uv run python -m workforce allow C:\path\to\repo   # let workers work on a project
uv run python -m workforce run coder "..." --difficulty heavy   # force a tier
uv run python -m workforce models | probe | smoke | bench | engine-test   # provider/engine tools
uv run python -m pytest -q                         # offline tests
```
Always use `uv run python -m ...`: Smart App Control blocks the unsigned `.exe` launchers (e.g. `pytest.exe`).

Each delegated task runs in its own background process (`run-task`), so it survives a VS Code reload;
`max_concurrent: 1` in `policies.yaml` keeps the CPU free (extra tasks wait as `queued`).
Prompts, reports and logs per task: `runs/<task-id>/`.

---

## UI work (Phases 6–7)
- **Playwright tests:** give the `qa` role the task; if the project has a `test:e2e` (or `e2e`) npm
  script, `verify_task` runs it automatically.
- **Seeing the page:** Claude can check pages itself with the Playwright MCP server. Add it once with
  `claude mcp add --scope user playwright -- npx @playwright/mcp@<version>` (pin a version).
- **Figma → UI:** Claude reads the frame through the Figma MCP (already connected), writes the spec
  into `delegate_task(role="uiux", ...)`, then compares a Playwright screenshot with the Figma one.
  Workers never get Figma access.

---

## Layout
| Path | What |
|---|---|
| `config/models.yaml` | providers, logical models, fallback chains |
| `config/agents.yaml` | roles: persona, chain, tools, default engine |
| `config/engines.yaml` | CLI tool workers (codex, cursor) |
| `config/policies.yaml` | limits, allowed projects, denied files, privacy, merge approval |
| `src/workforce/tasks.py` | delegate / run / revise / verify / merge / cleanup |
| `src/workforce/ledger.py` | SQLite task ledger + events + feedback |
| `src/workforce/sandbox.py` | git worktrees, path safety, commit, diff, merge |
| `src/workforce/verify.py` | deterministic checks (.workforce.yaml or auto-detected) |
| `src/workforce/mcp_server.py` | the MCP server Claude Code talks to |
| `src/workforce/tools/` | CrewAI tools: web search, file read/list/write (scoped to the worktree) |
| `src/workforce/llm/`, `engines/` | fallback router, Codex/Cursor adapters |
| `tests/` | offline tests (fake LLMs, fake engines, real git) |
| `workspace/`, `runs/` | task worktrees, logs and the ledger (git-ignored) |

## Security
- Keys live only in `.env` (git-ignored); Claude Code is denied from reading it. `doctor` fails if it's ever tracked.
- Workers never see your keys: anything that looks like a key or token is stripped from their environment,
  and from the environment of verification commands.
- Every coding task gets its own worktree and branch. Your checkout changes only through `merge_task`.
- Denied files (`.env`, keys, `.git`, `node_modules`, ...) can't be read by the CrewAI tools and are never
  committed from a worktree. Changes to tests, configs and lockfiles are flagged as "review closely".
- Codex runs with `--sandbox workspace-write` (never the bypass flags); Cursor runs read-only with a
  per-run rule file that denies shell, web, MCP and writes.
- **Limit:** like any AI coding tool, Codex and Cursor can *read* files your Windows user can read, and
  verification runs the project's own test code on your PC. Don't keep secrets in plain files in delegated projects.

## Troubleshooting
| Problem | Fix |
|---|---|
| `An Application Control policy has blocked this file` | Smart App Control: use `uv run python -m <tool>`, not `<tool>.exe` |
| `project ... is not in allowed_project_roots` | add the folder in `config/policies.yaml` |
| A task stays `queued` | another task is running (`max_concurrent: 1`); see `workforce tasks` |
| `worker process died` | see `runs/<task-id>/worker.log` |
| Groq `HTTP 403` with an empty message | Cloudflare blocked the User-Agent (already handled in `providers.py`) |
| `provider:ollama not reachable` | start the Ollama app |
| Codex/Cursor "not logged in" | `codex login` / `agent login` once in a terminal |
| `workforce` tools missing in Claude | `claude mcp get workforce`, then reload VS Code |
