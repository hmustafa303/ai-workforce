import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from workforce.settings import Settings

FIXTURE_CONFIG = Path(__file__).parent / "fixtures" / "config"


@pytest.fixture
def cfg_root(tmp_path):
    """A writable copy of the frozen test config (tests/fixtures/config).

    It is also a git repo with .env ignored, so doctor's repo checks pass.
    """
    root = tmp_path / "wf"  # sibling folders of it can act as user projects
    shutil.copytree(FIXTURE_CONFIG, root / "config")
    (root / ".gitignore").write_text(".env\n", encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    return root


def make_project(path, files=None):
    """A small committed git repo to delegate work on."""
    path.mkdir(parents=True, exist_ok=True)
    files = files or {"README.md": "# demo\n"}
    for rel, body in files.items():
        (path / rel).parent.mkdir(parents=True, exist_ok=True)
        (path / rel).write_text(body, encoding="utf-8")
    ident = ["-c", "user.name=t", "-c", "user.email=t@localhost"]
    subprocess.run(["git", "init", "-q", "-b", "main", str(path)], check=True)
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", *ident, "commit", "-qm", "init"], cwd=path, check=True)
    return path


def edit_config(root, name, fn):
    path = root / "config" / f"{name}.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    fn(data)
    path.write_text(yaml.safe_dump(data), encoding="utf-8")


def load_settings(root):
    return Settings.load(root, load_env=False)


def set_models(root, **ids):
    """Set concrete model ids, e.g. set_models(root, cloud_fast="gemini-x")."""
    def fn(d):
        for name, model_id in ids.items():
            d["models"][name]["model"] = model_id
    edit_config(root, "models", fn)
