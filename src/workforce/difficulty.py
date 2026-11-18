"""Pick a difficulty tier (light / standard / heavy) for a task.

Deterministic rules, no LLM call (so classifying costs no tokens), and the
reasons are returned so the Parent can see why. The Parent can always
override with an explicit difficulty.

  light     small, local, mechanical: typo, rename, docstring, a summary, a quick lookup
  standard  a normal feature or fix
  heavy     cross-cutting or risky: architecture, refactor, auth, payments, security,
            migrations, concurrency, performance, or a large/multi-file task
"""

from __future__ import annotations

import re

from workforce.settings import TIERS

HEAVY_WORDS = (
    "architecture", "architect", "refactor", "redesign", "rewrite", "migrate", "migration",
    "security", "vulnerab", "auth", "oauth", "login", "permission", "payment", "stripe",
    "checkout", "billing", "concurren", "race condition", "deadlock", "thread", "async",
    "performance", "optimi", "memory leak", "scalab", "database schema", "schema change",
    "end-to-end", "e2e", "integration", "multi-file", "across the codebase", "whole project",
    "entire", "from scratch", "complex", "root cause", "intermittent", "flaky",
)
LIGHT_WORDS = (
    "typo", "rename", "comment", "docstring", "readme", "format", "reformat", "lint fix",
    "spelling", "wording", "translate", "summar", "one-line", "one line", "small", "simple",
    "quick", "minor", "tiny", "bump version", "list ", "what is", "which version",
)

_WORD = re.compile(r"\w+")


def _hits(text: str, words: tuple[str, ...]) -> list[str]:
    return [w.strip() for w in words if w in text]


def classify(instructions: str, *, role: str = "", write: bool = False,
             acceptance: list[str] | None = None,
             context_files: list[str] | None = None) -> tuple[str, list[str]]:
    """Return (tier, reasons)."""
    text = f" {instructions.lower()} "
    words = len(_WORD.findall(instructions))
    score, reasons = 0, []

    heavy = _hits(text, HEAVY_WORDS)
    if heavy:
        score += 2 if len(heavy) == 1 else 3
        reasons.append(f"heavy topic: {', '.join(heavy[:4])}")
    light = _hits(text, LIGHT_WORDS)
    if light and not heavy:
        score -= 2
        reasons.append(f"light topic: {', '.join(light[:4])}")

    if words > 250:
        score += 2
        reasons.append(f"long brief ({words} words)")
    elif words > 100:
        score += 1
        reasons.append(f"medium brief ({words} words)")
    elif words < 25 and not write:  # a short brief can still mean real code work
        score -= 1
        reasons.append(f"short brief ({words} words)")

    n_ctx, n_acc = len(context_files or []), len(acceptance or [])
    if n_ctx > 4:
        score += 1
        reasons.append(f"{n_ctx} context files")
    if n_acc > 4:
        score += 1
        reasons.append(f"{n_acc} acceptance criteria")
    if write:
        score += 1
        reasons.append("changes code")
    elif role == "research":
        reasons.append("read-only research")

    tier = "light" if score <= 0 else "standard" if score <= 2 else "heavy"
    return tier, reasons or ["no strong signals"]


def escalate(tier: str | None) -> str:
    """One tier up (heavy stays heavy)."""
    i = TIERS.index(tier) if tier in TIERS else 1
    return TIERS[min(i + 1, len(TIERS) - 1)]
