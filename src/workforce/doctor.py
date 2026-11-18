"""Health checks for the workforce: `workforce doctor`.

Never prints secret values, only whether a secret is set.
"""

from __future__ import annotations

import importlib
import importlib.metadata as metadata
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from workforce.llm import providers
from workforce.settings import PROJECT_ROOT, ConfigError, Settings, secret_present

OK, WARN, FAIL = "ok", "warn", "fail"
REQUIRED_PACKAGES = ("crewai", "mcp")


@dataclass(frozen=True)
class Check:
    name: str
    status: str
    detail: str


def run_checks(root: Path = PROJECT_ROOT, *, live: bool = True) -> list[Check]:
    checks = [_check_python(), *_check_packages()]
    try:
        settings = Settings.load(root)
    except ConfigError as exc:
        checks.extend(Check("config", FAIL, err) for err in exc.errors)
        return checks
    checks.append(Check(
        "config", OK,
        f"{len(settings.agents.agents)} agents, {len(settings.models.models)} models, "
        f"{len(settings.models.chains)} chains",
    ))
    provider_checks, available = _check_providers(settings, live=live)
    checks += provider_checks
    checks += _check_models(settings, available)
    checks += _check_repo(settings.root)
    checks += _check_policies(settings)
    checks += _check_engines(settings)
    checks += _check_ledger(settings)
    return checks


def _check_python() -> Check:
    v = sys.version_info
    status = OK if (3, 10) <= (v.major, v.minor) < (3, 14) else FAIL
    return Check("python", status, f"{v.major}.{v.minor}.{v.micro} (CrewAI needs >=3.10,<3.14)")


def _check_packages() -> list[Check]:
    out = []
    for pkg in REQUIRED_PACKAGES:
        try:
            importlib.import_module(pkg)
            out.append(Check(f"package:{pkg}", OK, metadata.version(pkg)))
        except Exception as exc:  # noqa: BLE001 - report any import failure
            out.append(Check(f"package:{pkg}", FAIL, f"import failed: {exc}"))
    return out


def _check_providers(s: Settings, *, live: bool = True
                     ) -> tuple[list[Check], dict[str, set[str]]]:
    """Probe each enabled provider. Returns checks + models known to be available.

    With `live`, one configured cloud model per provider also gets a tiny real
    prompt: a key can be valid for listing yet refused for generation.
    """
    out: list[Check] = []
    available: dict[str, set[str]] = {}
    for name, p in s.models.providers.items():
        if not p.enabled:
            continue
        if p.api_key_env and not secret_present(p.api_key_env):
            out.append(Check(f"provider:{name}", WARN, f"{p.api_key_env} not set in .env"))
            continue
        key = os.environ[p.api_key_env].strip() if p.api_key_env else None
        try:
            if p.type == "ollama":
                url = p.resolved_base_url() or "http://localhost:11434"
                version = providers.ollama_version(url)
            models = providers.list_models(p, key)
        except providers.ProviderError as exc:
            # A key that is set but rejected is a real failure; a stopped
            # local server is only a warning.
            out.append(Check(f"provider:{name}", WARN if p.local else FAIL, str(exc)))
            continue
        available[name] = set(models)
        if p.type == "ollama":
            out.append(Check(f"provider:{name}", OK,
                             f"Ollama {version} at {url}; {len(models)} models pulled"))
        else:
            out.append(Check(f"provider:{name}", OK,
                             f"{p.api_key_env} is set and valid; {len(models)} models listed"))
        if live and not p.local:
            out += _live_probe(s, name, p, key)
    return out, available


def _live_probe(s: Settings, name: str, p, key: str | None) -> list[Check]:
    configured = [m.model for m in s.models.models.values()
                  if m.provider == name and m.model]
    if not configured:
        return []
    r = providers.probe(p, key, configured[0])
    if r.ok:
        return [Check(f"live:{name}", OK, f"{configured[0]} answered in {r.seconds}s")]
    return [Check(f"live:{name}", FAIL, f"{configured[0]}: {r.detail}")]


def _model_available(model_id: str, available: set[str]) -> bool:
    return model_id in available or (":" not in model_id and f"{model_id}:latest" in available)


def _check_models(s: Settings, available: dict[str, set[str]]) -> list[Check]:
    out = []
    unset = []
    for name, m in s.models.models.items():
        provider = s.models.providers[m.provider]
        if not provider.enabled:
            out.append(Check(f"model:{name}", WARN, f"provider '{m.provider}' is disabled"))
        elif m.model is None:
            unset.append(name)
        elif m.provider in available and not _model_available(m.model, available[m.provider]):
            out.append(Check(f"model:{name}", FAIL,
                             f"'{m.model}' is not available on {m.provider}"))
        else:
            verified = "verified" if m.provider in available else "not verified"
            out.append(Check(f"model:{name}", OK, f"{m.provider}/{m.model} ({verified})"))
    if unset:
        out.append(Check("models", WARN, f"not chosen yet: {', '.join(unset)}"))
    for chain in s.models.chains:
        if not s.models.chain_has_local(chain):
            out.append(Check(f"chain:{chain}", WARN, "no local (Ollama) fallback"))
    return out


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=15,
        stdin=subprocess.DEVNULL,
    )


def _check_repo(root: Path) -> list[Check]:
    try:
        inside = _git(root, "rev-parse", "--is-inside-work-tree")
    except (OSError, subprocess.SubprocessError) as exc:
        return [Check("git", FAIL, f"git not usable: {exc}")]
    if inside.returncode != 0:
        return [Check("git", FAIL, f"{root} is not a git repository")]
    out = [Check("git", OK, "repository found")]
    ignored = _git(root, "check-ignore", "-q", ".env").returncode == 0
    out.append(Check(
        "secrets:gitignore",
        OK if ignored else FAIL,
        ".env is git-ignored" if ignored else ".env is NOT git-ignored. Fix .gitignore!",
    ))
    tracked = _git(root, "ls-files", "--error-unmatch", ".env").returncode == 0
    if tracked:
        out.append(Check("secrets:tracked", FAIL, ".env is tracked by git. Remove it!"))
    return out


def _check_policies(s: Settings) -> list[Check]:
    out = []
    roots = s.policies.paths.allowed_project_roots
    if not roots:
        out.append(Check("policy:projects", WARN,
                         "allowed_project_roots is empty: add your project folders in "
                         "config/policies.yaml so workers may work on them"))
    for r in roots:
        if not r.is_dir():
            out.append(Check("policy:projects", WARN, f"allowed root does not exist: {r}"))
    if not s.policies.merge.require_user_approval:
        out.append(Check("policy:merge", WARN, "merges no longer require your approval"))
    return out


def _check_engines(s: Settings) -> list[Check]:
    from workforce.engines import EngineUnavailable, get_engine

    out = []
    for name, spec in s.engines.engines.items():
        if not spec.enabled:
            out.append(Check(f"engine:{name}", WARN, "disabled in config/engines.yaml"))
            continue
        try:
            engine = get_engine(s, name)
            binary = engine.resolve_binary()
        except EngineUnavailable as exc:
            out.append(Check(f"engine:{name}", WARN, str(exc)))
            continue
        mode = "can write (sandboxed)" if engine.supports_write else "read-only"
        out.append(Check(f"engine:{name}", OK, f"{mode}; {binary.name} found"))
    used = {a.engine for a in s.agents.agents.values()} - {"crew"}
    for name in sorted(used - set(s.engines.engines)):
        out.append(Check(f"engine:{name}", FAIL, "used by an agent but not configured"))
    return out


def _check_ledger(s: Settings) -> list[Check]:
    from workforce.ledger import Ledger

    try:
        ledger = Ledger(s.resolve(s.policies.paths.ledger_db))
        active = len(ledger.list("running")) + len(ledger.list("queued"))
    except Exception as exc:  # noqa: BLE001 - report any storage problem
        return [Check("ledger", FAIL, f"cannot open task ledger: {exc}")]
    return [Check("ledger", OK, f"{ledger.path.name} ready; {active} active task(s)")]


_LABEL = {OK: "[ OK ]", WARN: "[WARN]", FAIL: "[FAIL]"}


def format_report(checks: list[Check]) -> str:
    lines = [f"{_LABEL[c.status]} {c.name}: {c.detail}" for c in checks]
    counts = {k: sum(c.status == k for c in checks) for k in (OK, WARN, FAIL)}
    lines.append("")
    lines.append(f"{counts[OK]} ok, {counts[WARN]} warnings, {counts[FAIL]} failures")
    return "\n".join(lines)
