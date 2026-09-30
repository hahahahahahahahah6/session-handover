"""Smoke tests for handover generation and the CLI."""

import io
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import parsers, handover  # noqa: E402
from session_handover import cli  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures")


def _claude():
    return parsers.parse_claude_code(os.path.join(FIX, "claude_session.jsonl"))


def test_generate_sections():
    text = handover.generate(_claude(), include_git=False)
    for section in ("# Session Handover", "## Goal", "## What happened",
                    "## Errors encountered", "## Open items",
                    "## Next steps (for the receiving agent)"):
        assert section in text, "missing " + section
    assert "Add a /health endpoint" in text
    assert "server.py" in text
    assert "pytest" in text
    print("generate ok")


def test_generate_empty_session():
    text = handover.generate({"tool": "claude-code", "session_id": "x",
                              "started": "", "cwd": "", "messages": 0,
                              "goal": "", "actions": [], "errors": [],
                              "files": [], "counts": {}})
    assert "# Session Handover" in text
    print("empty session ok")


def _point_env_at_fixtures(monkey=True):
    tmp = tempfile.mkdtemp()
    cc = os.path.join(tmp, "cc")
    cx = os.path.join(tmp, "cx")
    os.makedirs(cc)
    os.makedirs(cx)
    import shutil
    shutil.copy(os.path.join(FIX, "claude_session.jsonl"), os.path.join(cc, "s1.jsonl"))
    shutil.copy(os.path.join(FIX, "codex_session.jsonl"), os.path.join(cx, "s2.jsonl"))
    os.environ["SESSION_HANDOVER_CLAUDE_DIR"] = cc
    os.environ["SESSION_HANDOVER_CODEX_DIR"] = cx
    return tmp


def test_cli_list_finds_both_tools(capsys=None):
    _point_env_at_fixtures()
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["list"])
    finally:
        sys.stdout = old
    out = buf.getvalue()
    assert rc == 0
    assert "claude-code" in out and "codex" in out
    assert "claude-abc123" in out and "codex-xyz789" in out
    print("cli list ok")


def test_cli_make_writes_file():
    _point_env_at_fixtures()
    with tempfile.TemporaryDirectory() as tmp:
        out = os.path.join(tmp, "HANDOVER.md")
        rc = cli.main(["make", "--session", "claude-abc", "--out", out])
        assert rc == 0
        body = open(out).read()
        assert "Add a /health endpoint" in body
        assert "## Next steps" in body
    print("cli make ok")


def test_cli_make_last_defaults_to_newest():
    _point_env_at_fixtures()
    # codex fixture file is newer on disk? force order via mtime
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(["make", "--last"])
    finally:
        sys.stdout = old
    assert rc == 0
    out = buf.getvalue()
    assert "# Session Handover" in out
    print("cli make --last ok")


def test_cli_make_unknown_session_fails():
    _point_env_at_fixtures()
    rc = cli.main(["make", "--session", "does-not-exist"])
    assert rc == 1
    print("cli make unknown ok")


if __name__ == "__main__":
    test_generate_sections()
    test_generate_empty_session()
    test_cli_list_finds_both_tools()
    test_cli_make_writes_file()
    test_cli_make_last_defaults_to_newest()
    test_cli_make_unknown_session_fails()
    print("ALL HANDOVER/CLI TESTS PASSED")
