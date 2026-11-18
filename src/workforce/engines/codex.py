"""Codex CLI engine (ChatGPT login).

read-only  -> `--sandbox read-only`        (can read, cannot change anything)
write      -> `--sandbox workspace-write`  (writes only inside the workdir)
Never uses the dangerous bypass flags.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from workforce.engines.base import Engine, EngineUnavailable


class CodexEngine(Engine):
    def resolve_binary(self) -> Path:
        if self.spec.binary:
            path = Path(self.spec.binary)
            if path.is_file():
                return path
            raise EngineUnavailable(f"codex binary not found: {path}")
        found = shutil.which("codex")
        if found:
            return Path(found)
        # Bundled with the ChatGPT VS Code extension (signed by OpenAI).
        ext_root = Path(os.environ.get("USERPROFILE", "~")).expanduser() / ".vscode" / "extensions"
        bundled = sorted(ext_root.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"),
                         key=lambda p: p.stat().st_mtime)
        if bundled:
            return bundled[-1]
        raise EngineUnavailable("codex not found (install the ChatGPT VS Code extension "
                                "or `npm i -g @openai/codex`)")

    def command(self, binary: Path, workdir: Path, *, write: bool,
                out_file: Path) -> list[str]:
        cmd = [
            str(binary), "exec",
            "--sandbox", "workspace-write" if write else "read-only",
            "--cd", str(workdir),
            "--skip-git-repo-check",
            "--color", "never",
            "--ephemeral",
            "--output-last-message", str(out_file),
        ]
        if self.model:
            cmd += ["--model", self.model]
        if self.effort:
            cmd += ["-c", f'model_reasoning_effort="{self.effort}"']
        cmd.append("-")  # read the prompt from stdin
        return cmd

    def final_message(self, stdout: str, out_file: Path) -> str:
        if out_file.is_file():
            return out_file.read_text(encoding="utf-8", errors="replace").strip()
        return ""
