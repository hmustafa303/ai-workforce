from workforce.tools import build_tools
from workforce.tools.fs import ListFilesTool, ReadFileTool, WriteFileTool

DENIED = ["**/.env", "**/.git/**"]


def test_read_list_write_are_scoped(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("A = 1\n")
    (tmp_path / ".env").write_text("SECRET=1\n")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.js").write_text("x")
    root = str(tmp_path)

    listing = ListFilesTool(root=root, denied_globs=DENIED)._run(".")
    assert "src/a.py" in listing and ".env" not in listing and "node_modules" not in listing

    read = ReadFileTool(root=root, denied_globs=DENIED)
    assert read._run("src/a.py") == "A = 1\n"
    assert read._run(".env").startswith("ERROR") and "SECRET" not in read._run(".env")
    assert read._run("../outside.txt").startswith("ERROR")

    write = WriteFileTool(root=root, denied_globs=DENIED)
    assert write._run("src/b.py", "B = 2\n").startswith("wrote")
    assert (tmp_path / "src" / "b.py").read_text() == "B = 2\n"
    assert write._run("../evil.txt", "x").startswith("ERROR")
    assert write._run(".env", "x").startswith("ERROR")
    assert not (tmp_path.parent / "evil.txt").exists()


def test_search_log_and_unverified_sources(tmp_path, monkeypatch):
    from workforce.sources import unverified_sources
    from workforce.tools import web

    monkeypatch.setattr(web, "search", lambda q, n=5: [
        {"title": "SQLite WAL", "url": "https://sqlite.org/wal.html", "snippet": "..."}])
    log = tmp_path / "search-urls.txt"
    out = web.WebSearchTool(log_path=str(log))._run("sqlite wal")
    assert "https://sqlite.org/wal.html" in out
    report = ("WAL lets readers run during writes [1].\n"
              "[1] https://sqlite.org/wal.html.\n[2] https://made-up.example/page")
    assert unverified_sources(report, log) == ["https://made-up.example/page"]


def test_build_tools_respects_write_flag(tmp_path):
    names = ["web_search", "fs_read", "worktree_write"]
    ro = [t.name for t in build_tools(names, workdir=tmp_path, denied_globs=[],
                                      allow_write=False)]
    rw = [t.name for t in build_tools(names, workdir=tmp_path, denied_globs=[],
                                      allow_write=True)]
    assert "write_file" not in ro and "write_file" in rw
    assert [t.name for t in build_tools(names, workdir=None, denied_globs=[],
                                        allow_write=True)] == ["web_search"]
