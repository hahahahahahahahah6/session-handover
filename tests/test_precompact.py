"""Tests for the PreCompact auto-handover hook (v0.3)."""

import ast
import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import precompact  # noqa: E402
from session_handover import cli  # noqa: E402


def _claude_transcript(path, n_user_turns=3):
    """Write a synthetic Claude Code transcript with rules and TODOs."""
    lines = []
    for i in range(n_user_turns):
        lines.append({
            "type": "user",
            "sessionId": "abc-123",
            "timestamp": "2026-10-01T00:00:0%dZ" % i,
            "cwd": "/tmp",
            "message": {
                "role": "user",
                "content": [{"type": "text",
                             "text": "Turn %d: Never push to main without "
                                     "asking me first. Still need to write "
                                     "the deploy script." % i}],
            },
        })
        lines.append({
            "type": "assistant",
            "sessionId": "abc-123",
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Working on it."}],
            },
        })
    with open(path, "w", encoding="utf-8") as fh:
        for obj in lines:
            fh.write(json.dumps(obj) + "\n")


def test_parse_hook_input():
    info = precompact.parse_hook_input(
        json.dumps({"session_id": "s1",
                    "transcript_path": "/tmp/t.jsonl"}))
    assert info["session_id"] == "s1"
    assert info["transcript_path"] == "/tmp/t.jsonl"
    # camelCase tolerance
    info = precompact.parse_hook_input(
        json.dumps({"sessionId": "s2", "transcriptPath": "/x"}))
    assert info["session_id"] == "s2"
    assert info["transcript_path"] == "/x"
    # garbage -> empty, never raises
    assert precompact.parse_hook_input("not json{{") == {}
    assert precompact.parse_hook_input("") == {}
    assert precompact.parse_hook_input("[1,2]") == {}
    print("parse_hook_input ok")


def test_safe_session_id():
    assert precompact.safe_session_id("abc-123") == "abc-123"
    assert precompact.safe_session_id("../../etc/passwd") == "etc_passwd"
    assert precompact.safe_session_id("") == "unknown"
    assert "/" not in precompact.safe_session_id("a/b\\c")
    print("safe_session_id ok")


def test_run_hook_writes_handover_with_checklist():
    tmp = tempfile.mkdtemp()
    tpath = os.path.join(tmp, "t.jsonl")
    _claude_transcript(tpath)
    out_dir = os.path.join(tmp, "out")
    written = precompact.run_hook("abc-123", tpath, out_dir)
    assert written == os.path.join(out_dir, "HANDOVER.abc-123.md")
    with open(written, encoding="utf-8") as fh:
        text = fh.read()
    assert "# Session Handover" in text
    assert "## Goal" in text
    assert "## Durable items the next session must preserve" in text
    assert "Never push to main without asking me first." in text
    assert "[rule]" in text
    print("run_hook writes handover+checklist ok")


def test_run_hook_degraded_on_bad_transcript():
    tmp = tempfile.mkdtemp()
    tpath = os.path.join(tmp, "t.jsonl")
    with open(tpath, "w", encoding="utf-8") as fh:
        fh.write("this is not jsonl at all\n{{{bad\n")
    written = precompact.run_hook("s9", tpath, os.path.join(tmp, "out"))
    with open(written, encoding="utf-8") as fh:
        text = fh.read()
    assert "No messages could be parsed from this transcript" in text
    assert "Durable items the next session must preserve" in text
    print("degraded handover ok")


def test_hook_main_garbage_stdin_never_blocks():
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        assert precompact.hook_main("garbage{{{") == 0
        assert precompact.hook_main("") == 0
    finally:
        sys.stderr = old
    print("garbage stdin fail-open ok")


def test_hook_main_missing_transcript_never_blocks():
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = precompact.hook_main(json.dumps(
            {"session_id": "s1", "transcript_path": "/nonexistent/t.jsonl"}))
        assert rc == 0
    finally:
        sys.stderr = old
    print("missing transcript fail-open ok")


def test_hook_main_end_to_end():
    tmp = tempfile.mkdtemp()
    tpath = os.path.join(tmp, "t.jsonl")
    _claude_transcript(tpath)
    out_dir = os.path.join(tmp, "out")
    os.environ["SESSION_HANDOVER_DIR"] = out_dir
    old = sys.stderr
    sys.stderr = io.StringIO()
    try:
        rc = precompact.hook_main(json.dumps(
            {"session_id": "e2e-1", "transcript_path": tpath}))
    finally:
        sys.stderr = old
        del os.environ["SESSION_HANDOVER_DIR"]
    assert rc == 0
    assert os.path.isfile(os.path.join(out_dir, "HANDOVER.e2e-1.md"))
    print("hook_main end-to-end ok")


def test_extraction_window_caps_turns():
    tmp = tempfile.mkdtemp()
    tpath = os.path.join(tmp, "big.jsonl")
    _claude_transcript(tpath, n_user_turns=precompact.EXTRACTION_WINDOW + 50)
    turns = precompact._recent_turns(tpath, "claude-code")
    assert len(turns) == precompact.EXTRACTION_WINDOW, len(turns)
    print("extraction window cap ok")


def _fake_home():
    tmp = tempfile.mkdtemp()
    os.environ["HOME"] = tmp
    return tmp


def test_hook_install_uninstall_idempotent():
    home = _fake_home()
    settings = os.path.join(home, ".claude", "settings.json")
    # pre-existing settings must be preserved
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump({"theme": "dark", "hooks": {"UserPromptSubmit": []}}, fh)

    cmd1 = precompact.hook_install()
    cmd2 = precompact.hook_install()
    assert cmd1 == cmd2
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["theme"] == "dark"  # other keys untouched
    entries = data["hooks"]["PreCompact"]
    matches = [h for e in entries for h in e["hooks"]
               if h.get("command") == cmd1]
    assert len(matches) == 1, "install must be idempotent, got %d" % len(matches)

    assert precompact.hook_uninstall() is True
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    assert "PreCompact" not in data["hooks"]
    assert data["theme"] == "dark"
    # uninstall again: no-op
    assert precompact.hook_uninstall() is False
    print("hook_install/uninstall idempotent ok")


def test_hook_install_merges_with_existing_precompact():
    home = _fake_home()
    settings = os.path.join(home, ".claude", "settings.json")
    os.makedirs(os.path.dirname(settings), exist_ok=True)
    with open(settings, "w", encoding="utf-8") as fh:
        json.dump({"hooks": {"PreCompact": [
            {"hooks": [{"type": "command",
                        "command": "echo existing-hook"}]}]}}, fh)
    precompact.hook_install()
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    commands = [h["command"] for e in data["hooks"]["PreCompact"]
                for h in e["hooks"]]
    assert "echo existing-hook" in commands  # not clobbered
    assert len(commands) == 2
    precompact.hook_uninstall()
    with open(settings, encoding="utf-8") as fh:
        data = json.load(fh)
    commands = [h["command"] for e in data["hooks"]["PreCompact"]
                for h in e["hooks"]]
    assert commands == ["echo existing-hook"]  # only ours removed
    print("hook_install merges ok")


def test_cli_hook_install_uninstall():
    _fake_home()
    assert cli.main(["hook-install", "precompact"]) == 0
    assert cli.main(["hook-uninstall", "precompact"]) == 0
    assert cli.main(["hook-uninstall", "precompact"]) == 0  # idempotent
    print("cli hook-install/uninstall ok")


def test_hook_install_uses_running_interpreter_not_plain_python3():
    """Regression: hook-install must register sys.executable, not "python3".

    With a pipx/venv/uv install, the system python3 has no session_handover
    package, so a hardcoded "python3 <shim>" hook fail-opens on every
    /compact and the user believes they are protected. Install the package
    into a real venv, run hook-install with the venv's interpreter, and
    assert the registered command points at that interpreter -- then run
    the registered command and assert the hook actually fires.
    """
    import shutil
    import subprocess

    if shutil.which("python3") is None:
        print("SKIP: no python3 for venv test")
        return
    repo = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    with tempfile.TemporaryDirectory() as tmp:
        venv = os.path.join(tmp, "venv")
        subprocess.run([sys.executable, "-m", "venv", venv], check=True,
                       capture_output=True)
        vpy = os.path.join(venv, "bin", "python")
        subprocess.run([vpy, "-m", "pip", "install", "--quiet", repo],
                       check=True, capture_output=True)
        home = os.path.join(tmp, "home")
        os.makedirs(os.path.join(home, ".claude"), exist_ok=True)
        out_dir = os.path.join(tmp, "out")
        env = dict(os.environ, HOME=home, SESSION_HANDOVER_DIR=out_dir)
        cli_bin = os.path.join(venv, "bin", "session-handover")
        subprocess.run([cli_bin, "hook-install", "precompact"],
                       check=True, env=env, capture_output=True)
        with open(os.path.join(home, ".claude", "settings.json"),
                  encoding="utf-8") as fh:
            data = json.load(fh)
        commands = [h["command"] for e in data["hooks"]["PreCompact"]
                    for h in e["hooks"]]
        assert len(commands) == 1, commands
        cmd = commands[0]
        # Must invoke the venv's interpreter -- never a bare "python3".
        assert not cmd.startswith("python3 "), cmd
        assert cmd.startswith(vpy + " "), cmd

        # The registered command really fires under the venv interpreter:
        # the package is importable there, so a HANDOVER file is written.
        tpath = os.path.join(tmp, "t.jsonl")
        _claude_transcript(tpath)
        proc = subprocess.run(
            cmd, shell=True,
            input=json.dumps({"session_id": "venv-e2e",
                              "transcript_path": tpath}),
            capture_output=True, text=True, env=env)
        assert proc.returncode == 0, proc.stderr
        written = os.path.join(out_dir, "HANDOVER.venv-e2e.md")
        assert os.path.isfile(written), \
            "hook did not fire: %s" % proc.stderr
        with open(written, encoding="utf-8") as fh:
            assert "# Session Handover" in fh.read()
    print("hook_install uses running interpreter ok")


def test_shim_is_stdlib_only_single_file():
    shim = os.path.join(os.path.dirname(__file__), "..", "hooks",
                        "precompact.py")
    with open(shim, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    stdlib = {"os", "sys"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                assert a.name.split(".")[0] in stdlib, a.name
        elif isinstance(node, ast.ImportFrom):
            assert (node.module or "").split(".")[0] in ("session_handover",), \
                node.module
    print("shim stdlib-only ok")


if __name__ == "__main__":
    test_parse_hook_input()
    test_safe_session_id()
    test_run_hook_writes_handover_with_checklist()
    test_run_hook_degraded_on_bad_transcript()
    test_hook_main_garbage_stdin_never_blocks()
    test_hook_main_missing_transcript_never_blocks()
    test_hook_main_end_to_end()
    test_extraction_window_caps_turns()
    test_hook_install_uninstall_idempotent()
    test_hook_install_merges_with_existing_precompact()
    test_cli_hook_install_uninstall()
    test_hook_install_uses_running_interpreter_not_plain_python3()
    test_shim_is_stdlib_only_single_file()
    print("ALL PRECOMPACT TESTS PASSED")
