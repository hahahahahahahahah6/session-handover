"""Tests for the SessionEnd hook subcommand."""

import io
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import sessionend  # noqa: E402
from session_handover import cli  # noqa: E402


def _transcript():
    """A small claude-shaped transcript with todos, decisions, rules."""
    tmp = tempfile.mkdtemp()
    path = os.path.join(tmp, "sess.jsonl")
    turns = [
        ("user", "Add rate limiting to the API. We decided to use Redis "
                 "for the limiter. Never deploy on Fridays."),
        ("assistant", "I'll add the limiter now. Still need to write the "
                      "tests for it."),
        ("user", "Also TODO: update the docs page."),
        ("assistant", "Limiter is in. I prefer pytest for new tests."),
    ]
    with open(path, "w") as fh:
        for i, (role, text) in enumerate(turns):
            fh.write(json.dumps({
                "type": role, "sessionId": "sess-1",
                "message": {"role": role,
                            "content": [{"type": "text", "text": text}]},
            }) + "\n")
    return path


def test_extract_handoff_buckets():
    data = sessionend.extract_handoff(_transcript(), "claude-code")
    assert "rate limiting" in data["goal"].lower()
    todos = [i["text"] for i in data["unfinished"]]
    assert any("tests" in t for t in todos), todos
    assert any("docs" in t for t in todos), todos
    decisions = [i["text"] for i in data["decisions"]]
    assert any("Redis" in d for d in decisions), decisions
    follow = [i["text"] for i in data["follow_up"]]
    assert any("Fridays" in f for f in follow), follow
    assert any("pytest" in f for f in follow), follow
    print("extract buckets ok")


def test_run_sessionend_json():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "h.json")
    path = sessionend.run_sessionend("sess-1", _transcript(), out=out,
                                     fmt="json")
    assert path == out
    data = json.load(open(out))
    assert data["format"] == "session-handover-sessionend"
    assert data["session_id"] == "sess-1"
    assert len(data["unfinished_tasks"]) >= 2
    assert len(data["key_decisions"]) >= 1
    print("run json ok")


def test_run_sessionend_markdown():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "h.md")
    sessionend.run_sessionend("sess-1", _transcript(), out=out,
                              fmt="markdown")
    body = open(out).read()
    for section in ("# Session Handoff", "## Unfinished tasks",
                    "## Key decisions", "## Follow-ups & constraints"):
        assert section in body, "missing " + section
    assert "Redis" in body
    print("run markdown ok")


def test_run_sessionend_out_dir():
    tmp = tempfile.mkdtemp()
    outdir = os.path.join(tmp, "d")
    os.makedirs(outdir)
    path = sessionend.run_sessionend("sess/1:weird", _transcript(),
                                     out=outdir, fmt="json")
    assert os.path.isfile(path)
    assert "sess_1_weird" in path  # sanitized
    print("run out-dir ok")


def test_run_sessionend_bad_transcript_degrades():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "h.json")
    path = sessionend.run_sessionend("s", "/no/such/file.jsonl", out=out)
    data = json.load(open(out))
    assert data["session_id"] == "s"
    assert path == out
    print("run degraded ok")


def test_sessionend_main_garbage_stdin():
    rc = sessionend.sessionend_main(stdin_text="not json{{{",
                                    out=os.path.join(tempfile.mkdtemp(),
                                                     "h.json"))
    assert rc == 0
    print("main garbage stdin ok")


def test_sessionend_main_missing_transcript():
    rc = sessionend.sessionend_main(
        stdin_text=json.dumps({"session_id": "x",
                               "transcript_path": "/nope.jsonl"}),
        out=os.path.join(tempfile.mkdtemp(), "h.json"))
    assert rc == 0
    print("main missing transcript ok")


def test_sessionend_main_happy_path():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "h.json")
    stdin = json.dumps({"session_id": "sess-9",
                        "transcript_path": _transcript()})
    buf = io.StringIO()
    old = sys.stderr
    sys.stderr = buf
    try:
        rc = sessionend.sessionend_main(stdin_text=stdin, out=out)
    finally:
        sys.stderr = old
    assert rc == 0
    assert "wrote" in buf.getvalue()
    assert json.load(open(out))["session_id"] == "sess-9"
    print("main happy ok")


def _patch_settings(monkey_target, tmp_settings):
    monkey_target["orig"] = sessionend.settings_path
    sessionend.settings_path = lambda: tmp_settings


def _restore_settings(monkey_target):
    sessionend.settings_path = monkey_target["orig"]


def test_sessionend_install_uninstall():
    tmp = tempfile.mkdtemp()
    settings = os.path.join(tmp, "settings.json")
    box = {}
    _patch_settings(box, settings)
    try:
        cmd1 = sessionend.sessionend_install()
        cmd2 = sessionend.sessionend_install()
        assert cmd1 == cmd2  # idempotent, no duplicate
        data = json.load(open(settings))
        entries = data["hooks"]["SessionEnd"]
        assert len(entries) == 1
        nested = entries[0]["hooks"]
        assert len(nested) == 1
        assert nested[0]["command"] == cmd1
        assert nested[0]["type"] == "command"
        assert sessionend.sessionend_uninstall() is True
        assert sessionend.sessionend_uninstall() is False
        data = json.load(open(settings))
        assert "SessionEnd" not in data.get("hooks", {})
    finally:
        _restore_settings(box)
    print("install/uninstall ok")


def test_sessionend_install_preserves_others():
    tmp = tempfile.mkdtemp()
    settings = os.path.join(tmp, "settings.json")
    with open(settings, "w") as fh:
        json.dump({"hooks": {"PreCompact": [{"hooks": [
            {"type": "command", "command": "other"}]}]}}, fh)
    box = {}
    _patch_settings(box, settings)
    try:
        sessionend.sessionend_install()
        data = json.load(open(settings))
        assert len(data["hooks"]["PreCompact"]) == 1
        assert len(data["hooks"]["SessionEnd"]) == 1
    finally:
        _restore_settings(box)
    print("install preserves ok")


def _run_cli(argv, stdin_text=None):
    buf_out, buf_err = io.StringIO(), io.StringIO()
    old_out, old_err, old_in = sys.stdout, sys.stderr, sys.stdin
    sys.stdout, sys.stderr = buf_out, buf_err
    if stdin_text is not None:
        sys.stdin = io.StringIO(stdin_text)
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout, sys.stderr, sys.stdin = old_out, old_err, old_in
    return rc, buf_out.getvalue(), buf_err.getvalue()


def test_cli_hook_sessionend():
    tmp = tempfile.mkdtemp()
    out = os.path.join(tmp, "h.json")
    rc, _o, err = _run_cli(
        ["hook", "--session-end", "--out", out],
        stdin_text=json.dumps({"session_id": "cli-1",
                               "transcript_path": _transcript()}))
    assert rc == 0, err
    assert json.load(open(out))["session_id"] == "cli-1"
    print("cli hook ok")


def test_cli_hook_no_flag():
    rc, _o, _e = _run_cli(["hook"])
    assert rc == 2
    print("cli hook no-flag ok")


def test_cli_hook_install_sessionend():
    tmp = tempfile.mkdtemp()
    settings = os.path.join(tmp, "settings.json")
    box = {}
    _patch_settings(box, settings)
    try:
        rc, out, _e = _run_cli(["hook-install", "sessionend"])
        assert rc == 0, out
        assert "SessionEnd" in out
        rc, out, _e = _run_cli(["hook-uninstall", "sessionend"])
        assert rc == 0
        assert "Removed" in out
    finally:
        _restore_settings(box)
    print("cli hook-install ok")


if __name__ == "__main__":
    test_extract_handoff_buckets()
    test_run_sessionend_json()
    test_run_sessionend_markdown()
    test_run_sessionend_out_dir()
    test_run_sessionend_bad_transcript_degrades()
    test_sessionend_main_garbage_stdin()
    test_sessionend_main_missing_transcript()
    test_sessionend_main_happy_path()
    test_sessionend_install_uninstall()
    test_sessionend_install_preserves_others()
    test_cli_hook_sessionend()
    test_cli_hook_no_flag()
    test_cli_hook_install_sessionend()
    print("ALL SESSIONEND TESTS PASSED")
