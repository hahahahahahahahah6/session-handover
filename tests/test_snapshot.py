"""Tests for pre-compaction snapshots and the post-compact state audit."""

import hashlib
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from session_handover import snapshot  # noqa: E402
from session_handover import cli  # noqa: E402


def _env():
    """Build a fake skills dir + CLAUDE.md + MEMORY.md. Returns dict of paths."""
    tmp = tempfile.mkdtemp()
    skills = os.path.join(tmp, "skills")
    for name in ("alpha", "beta"):
        d = os.path.join(skills, name)
        os.makedirs(d)
        body = ("# %s skill\n\nBody of %s. " % (name, name)) * 12 + "\n"
        with open(os.path.join(d, "SKILL.md"), "w") as fh:
            fh.write(body)
    claude_md = os.path.join(tmp, "CLAUDE.md")
    with open(claude_md, "w") as fh:
        fh.write("# Project rules\n\n- Be nice.\n")
    memory_md = os.path.join(tmp, "MEMORY.md")
    with open(memory_md, "w") as fh:
        fh.write("# Memory\n\nSome notes.\n")
    return {"tmp": tmp, "skills": skills, "claude_md": claude_md,
            "memory_md": memory_md}


def _take(env):
    return snapshot.take_snapshot(skills_dirs=[env["skills"]],
                                  claude_md=env["claude_md"],
                                  memory_md=env["memory_md"])


def _sha(path):
    with open(path, "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def test_snapshot_captures_everything():
    env = _env()
    snap = _take(env)
    assert snap["format"] == "session-handover-snapshot"
    names = [s["name"] for s in snap["skills"]]
    assert names == ["alpha", "beta"], names
    for s in snap["skills"]:
        assert s["sha256"] == _sha(s["path"])
        assert "Body of" in s["content"]
    assert snap["claude_md"]["sha256"] == _sha(env["claude_md"])
    assert snap["memory_md"]["sha256"] == _sha(env["memory_md"])
    assert "taken_at" in snap
    print("snapshot capture ok")


def test_snapshot_missing_files_record_none():
    tmp = tempfile.mkdtemp()
    snap = snapshot.take_snapshot(skills_dirs=[os.path.join(tmp, "nope")],
                                  claude_md=os.path.join(tmp, "no.md"),
                                  memory_md=None)
    assert snap["skills"] == []
    assert snap["claude_md"] is None
    print("snapshot missing ok")


def test_snapshot_roundtrip():
    env = _env()
    snap = _take(env)
    path = os.path.join(env["tmp"], "snap.json")
    snapshot.save_snapshot(snap, path)
    back = snapshot.load_snapshot(path)
    assert back["skills"] == snap["skills"]
    print("snapshot roundtrip ok")


def test_load_snapshot_rejects_garbage():
    path = os.path.join(tempfile.mkdtemp(), "x.json")
    with open(path, "w") as fh:
        fh.write('{"nope": 1}')
    try:
        snapshot.load_snapshot(path)
    except ValueError:
        print("snapshot reject ok")
        return
    raise AssertionError("should have raised")


def test_audit_clean():
    env = _env()
    snap = _take(env)
    result = snapshot.audit_against_snapshot(snap)
    assert result["problems"] == [], result["problems"]
    assert len(result["ok"]) == 4  # 2 skills + claude + memory
    print("audit clean ok")


def test_audit_missing_skill():
    env = _env()
    snap = _take(env)
    os.remove(os.path.join(env["skills"], "alpha", "SKILL.md"))
    result = snapshot.audit_against_snapshot(snap)
    assert len(result["problems"]) == 1
    p = result["problems"][0]
    assert p["category"] == "missing-skill-body"
    assert "alpha" in p["detail"]
    assert "snapshot" in p["suggestion"].lower()
    print("audit missing skill ok")


def test_audit_gutted_skill():
    env = _env()
    snap = _take(env)
    with open(os.path.join(env["skills"], "beta", "SKILL.md"), "w") as fh:
        fh.write("")
    result = snapshot.audit_against_snapshot(snap)
    cats = [p["category"] for p in result["problems"]]
    assert "missing-skill-body" in cats, cats
    print("audit gutted skill ok")


def test_audit_stale_reattach():
    env = _env()
    snap = _take(env)
    taken = snapshot._taken_at_ts(snap)
    path = os.path.join(env["skills"], "alpha", "SKILL.md")
    # Simulate: file edited after the snapshot, then an older version
    # re-attached over it (content back to the snapshot version).
    with open(path, "w") as fh:
        fh.write(("# alpha skill\n\nBody of alpha. " * 12) + "\n")
    os.utime(path, (taken + 100, taken + 100))
    result = snapshot.audit_against_snapshot(snap)
    assert len(result["problems"]) == 1
    p = result["problems"][0]
    assert p["category"] == "stale-reattach"
    assert "byte-identical" in p["detail"]
    print("audit stale reattach ok")


def test_audit_legit_change_is_not_a_problem():
    env = _env()
    snap = _take(env)
    with open(os.path.join(env["skills"], "alpha", "SKILL.md"), "w") as fh:
        fh.write("# alpha skill\n\nCompletely new improved body here, with "
                 "lots of genuinely different documentation text.\n")
    result = snapshot.audit_against_snapshot(snap)
    assert result["problems"] == []
    assert any(e["state"] == "changed" for e in result["ok"])
    print("audit legit change ok")


def test_audit_claude_new_text_no_summary():
    env = _env()
    snap = _take(env)
    with open(env["claude_md"], "a") as fh:
        fh.write("- New rule: always run lint first.\n")
    result = snapshot.audit_against_snapshot(snap)
    probs = [p for p in result["problems"]
             if p["category"] == "claude-md-new-text-lost"]
    assert len(probs) == 1
    p = probs[0]
    assert p["severity"] == "check"
    assert "always run lint first" in p["suggestion"]
    assert p["diff"], "diff should be attached"
    print("audit claude new text ok")


def test_audit_claude_new_text_lost_from_summary():
    env = _env()
    snap = _take(env)
    with open(env["claude_md"], "a") as fh:
        fh.write("- New rule: always run lint first.\n")
    # Summary carries only the OLD text.
    result = snapshot.audit_against_snapshot(
        snap, summary_texts=["# Project rules - Be nice. nothing else here"])
    probs = [p for p in result["problems"]
             if p["category"] == "claude-md-new-text-lost"]
    assert len(probs) == 1
    assert probs[0]["severity"] == "lost"
    print("audit claude lost-from-summary ok")


def test_audit_claude_new_text_survived_in_summary():
    env = _env()
    snap = _take(env)
    with open(env["claude_md"], "a") as fh:
        fh.write("- New rule: always run lint first.\n")
    result = snapshot.audit_against_snapshot(
        snap, summary_texts=["# Project rules - Be nice. New rule: always "
                             "run lint first, added mid-session."])
    probs = [p for p in result["problems"]
             if p["category"] == "claude-md-new-text-lost"]
    assert probs == [], probs
    print("audit claude survived ok")


def test_audit_claude_missing():
    env = _env()
    snap = _take(env)
    os.remove(env["claude_md"])
    result = snapshot.audit_against_snapshot(snap)
    cats = [p["category"] for p in result["problems"]]
    assert "missing-file" in cats, cats
    print("audit claude missing ok")


def test_renderers():
    env = _env()
    snap = _take(env)
    os.remove(os.path.join(env["skills"], "alpha", "SKILL.md"))
    result = snapshot.audit_against_snapshot(
        snap, snapshot_path="/tmp/snap.json")
    term = snapshot.render_terminal(result)
    assert "MISSING SKILL BODY" in term
    assert "problems: 1" in term
    md = snapshot.render_markdown(result)
    assert "# Snapshot Audit" in md
    assert "MISSING SKILL BODY" in md
    print("renderers ok")


def _capture_cli(argv):
    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = cli.main(argv)
    finally:
        sys.stdout = old
    return rc, buf.getvalue()


def test_cli_snapshot_audit_roundtrip():
    env = _env()
    snap_path = os.path.join(env["tmp"], "snap.json")
    rc, out = _capture_cli(["snapshot", "--skills-dir", env["skills"],
                            "--claude-md", env["claude_md"],
                            "--memory", env["memory_md"],
                            "-o", snap_path])
    assert rc == 0, out
    assert os.path.isfile(snap_path)
    os.remove(os.path.join(env["skills"], "beta", "SKILL.md"))
    rc, out = _capture_cli(["audit", "--snapshot", snap_path])
    assert rc == 0
    assert "MISSING SKILL BODY" in out
    rc, _out2 = _capture_cli(["audit", "--snapshot", snap_path,
                              "--fail-on-problem"])
    assert rc == 1
    print("cli roundtrip ok")


def test_cli_snapshot_stdout():
    env = _env()
    rc, out = _capture_cli(["snapshot", "--skills-dir", env["skills"],
                            "--claude-md", env["claude_md"]])
    assert rc == 0
    data = json.loads(out)
    assert data["format"] == "session-handover-snapshot"
    print("cli snapshot stdout ok")


def test_cli_audit_bad_snapshot():
    rc, _out = _capture_cli(["audit", "--snapshot", "/no/such/file.json"])
    assert rc == 2
    print("cli audit bad snapshot ok")


def test_audit_skills_dir_override():
    env = _env()
    snap = _take(env)
    # Move alpha elsewhere; override dir should still find it.
    other = os.path.join(env["tmp"], "other")
    os.makedirs(os.path.join(other, "alpha"))
    with open(os.path.join(other, "alpha", "SKILL.md"), "w") as fh:
        fh.write(("# alpha skill\n\nBody of alpha. " * 12) + "\n")
    os.utime(os.path.join(other, "alpha", "SKILL.md"),
             (time.time() - 1000, time.time() - 1000))  # old mtime, no flag
    result = snapshot.audit_against_snapshot(
        snap, skills_dirs=[other, env["skills"]])
    cats = [p["category"] for p in result["problems"]]
    assert "missing-skill-body" not in cats, cats
    print("audit skills-dir override ok")


if __name__ == "__main__":
    test_snapshot_captures_everything()
    test_snapshot_missing_files_record_none()
    test_snapshot_roundtrip()
    test_load_snapshot_rejects_garbage()
    test_audit_clean()
    test_audit_missing_skill()
    test_audit_gutted_skill()
    test_audit_stale_reattach()
    test_audit_legit_change_is_not_a_problem()
    test_audit_claude_new_text_no_summary()
    test_audit_claude_new_text_lost_from_summary()
    test_audit_claude_new_text_survived_in_summary()
    test_audit_claude_missing()
    test_renderers()
    test_cli_snapshot_audit_roundtrip()
    test_cli_snapshot_stdout()
    test_cli_audit_bad_snapshot()
    test_audit_skills_dir_override()
    print("ALL SNAPSHOT TESTS PASSED")
