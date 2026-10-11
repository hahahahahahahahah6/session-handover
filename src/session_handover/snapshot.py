"""Pre-compaction state snapshots and post-compact state audit.

Compaction loses more than transcript turns. Observed failure modes
(rulestack, 8 compaction runs):
  - 2/8 runs lost a skill body: the skill was in context before /compact
    and gone after;
  - re-attached skill bodies came back as the OLD version even though the
    SKILL.md on disk had been edited mid-session;
  - CLAUDE.md edited mid-session, then /compact: the summary carried the
    OLD text and the new text was lost from context.

``snapshot`` freezes the pre-compaction state (skill bodies, CLAUDE.md,
MEMORY.md); ``audit --snapshot`` compares the current disk against that
snapshot after compaction and reports three mechanical problems:

  - missing-skill-body: in the snapshot, gone from disk now (or emptied);
  - stale-reattach: file was modified after the snapshot was taken but its
    current content is byte-identical to the snapshot -- an older version
    appears to have been re-attached over mid-session edits;
  - claude-md-new-text-lost: CLAUDE.md gained new lines after the snapshot;
    the added lines are shown so the agent can check them against the
    resumed context (optionally cross-checked against the compaction
    summary from the session transcript).

stdlib only. Fail-soft: unreadable files are skipped with a note, never a
crash. Snapshot content is stored verbatim so a missing skill body can be
restored from the snapshot file itself.
"""

import datetime
import difflib
import hashlib
import json
import os

from . import compact_audit
from . import parsers

SNAPSHOT_FORMAT = "session-handover-snapshot"
SNAPSHOT_FORMAT_VERSION = 1

# A file whose content matches the snapshot but whose mtime is clearly
# newer than the snapshot is suspicious: something rewrote it with the
# old version. Filesystems have ~1s mtime granularity, so require a
# comfortable margin before calling it a re-attach.
REATTACH_MTIME_EPSILON = 2.0

# A skill body that shrank to (near-)nothing is treated as lost.
GUTTED_BYTES = 64


def utcnow_iso():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Default path probing
# ---------------------------------------------------------------------------

def default_skills_dirs():
    """Skill directories to probe, in order. Only existing ones are kept."""
    home = os.path.expanduser("~")
    cwd = os.getcwd()
    candidates = [
        os.path.join(home, ".claude", "skills"),
        os.path.join(cwd, "skills"),
        os.path.join(cwd, ".claude", "skills"),
    ]
    seen = set()
    dirs = []
    for d in candidates:
        d = os.path.abspath(d)
        if d not in seen and os.path.isdir(d):
            seen.add(d)
            dirs.append(d)
    return dirs


def _first_existing(paths):
    for p in paths:
        if p and os.path.isfile(p):
            return os.path.abspath(p)
    return None


def default_claude_md():
    home = os.path.expanduser("~")
    return _first_existing([
        os.path.join(os.getcwd(), "CLAUDE.md"),
        os.path.join(home, ".claude", "CLAUDE.md"),
        os.path.join(home, "CLAUDE.md"),
    ])


def default_memory_md():
    home = os.path.expanduser("~")
    return _first_existing([
        os.path.join(os.getcwd(), "MEMORY.md"),
        os.path.join(home, ".claude", "MEMORY.md"),
        os.path.join(home, "MEMORY.md"),
    ])


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------

def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def read_file_bytes(path):
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def discover_skills(skills_dirs):
    """Find skills: <dir>/<name>/SKILL.md (nested) or <dir>/SKILL.md (flat).

    Returns a list of {"name", "path"} sorted by name. Depth is one level:
    only immediate children of the given dirs are considered.
    """
    found = []
    seen_paths = set()
    for base in skills_dirs or []:
        if not os.path.isdir(base):
            continue
        try:
            entries = sorted(os.listdir(base))
        except OSError:
            continue
        for entry in entries:
            full = os.path.join(base, entry)
            if os.path.isdir(full):
                skill_file = os.path.join(full, "SKILL.md")
                name = entry
            elif entry.upper() == "SKILL.MD":
                skill_file = full
                name = os.path.basename(os.path.abspath(base))
            else:
                continue
            skill_file = os.path.abspath(skill_file)
            if os.path.isfile(skill_file) and skill_file not in seen_paths:
                seen_paths.add(skill_file)
                found.append({"name": name, "path": skill_file})
    found.sort(key=lambda s: s["name"])
    return found


def file_record(path):
    """Record one file: hash, size, mtime, full content. None if unreadable."""
    data = read_file_bytes(path)
    if data is None:
        return None
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        mtime = 0.0
    return {
        "path": os.path.abspath(path),
        "sha256": _sha256_bytes(data),
        "bytes": len(data),
        "mtime": mtime,
        "content": data.decode("utf-8", errors="replace"),
    }


def take_snapshot(skills_dirs=None, claude_md=None, memory_md=None):
    """Freeze the pre-compaction state. Returns the snapshot dict.

    skills_dirs: list of dirs to scan, or None to probe defaults.
    claude_md / memory_md: explicit paths, or None to probe defaults.
    Explicitly-passed paths that do not exist are recorded as None (the
    audit then reports them as newly-appeared if they show up later).
    """
    if skills_dirs is None:
        skills_dirs = default_skills_dirs()
    skills = []
    for skill in discover_skills(skills_dirs):
        rec = file_record(skill["path"])
        if rec is not None:
            rec["name"] = skill["name"]
            skills.append(rec)
    if claude_md is None:
        claude_md = default_claude_md()
    if memory_md is None:
        memory_md = default_memory_md()
    now = utcnow_iso()
    return {
        "format": SNAPSHOT_FORMAT,
        "format_version": SNAPSHOT_FORMAT_VERSION,
        "taken_at": now,
        "cwd": os.getcwd(),
        "skills_dirs": [os.path.abspath(d) for d in (skills_dirs or [])],
        "skills": skills,
        "claude_md": file_record(claude_md) if claude_md else None,
        "memory_md": file_record(memory_md) if memory_md else None,
    }


def save_snapshot(snapshot, path):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


def load_snapshot(path):
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    if not isinstance(data, dict) or data.get("format") != SNAPSHOT_FORMAT:
        raise ValueError("not a session-handover snapshot: %s" % path)
    return data


def _taken_at_ts(snapshot):
    try:
        dt = datetime.datetime.fromisoformat(snapshot.get("taken_at", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return dt.timestamp()
    except Exception:
        return 0.0


# ---------------------------------------------------------------------------
# Audit: current disk vs snapshot
# ---------------------------------------------------------------------------

def _problem(category, path, detail, suggestion, diff="", severity="problem"):
    return {
        "category": category,
        "file": path,
        "detail": detail,
        "diff": diff,
        "suggestion": suggestion,
        "severity": severity,
    }


def _diff_summary(old_text, new_text):
    """Unified diff plus (added, removed) line counts, excluding headers."""
    old_lines = (old_text or "").splitlines()
    new_lines = (new_text or "").splitlines()
    diff_lines = list(difflib.unified_diff(
        old_lines, new_lines,
        fromfile="snapshot (pre-compaction)",
        tofile="current (post-compact)", lineterm=""))
    added = [l[1:] for l in diff_lines
             if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff_lines
               if l.startswith("-") and not l.startswith("---")]
    return "\n".join(diff_lines), added, removed


def _rescan_skill(name, skills_dirs):
    """Look for a skill by name in the current skills dirs (it may have moved)."""
    for skill in discover_skills(skills_dirs):
        if skill["name"] == name:
            return skill["path"]
    return None


def _audit_skills(snapshot, skills_dirs, taken_ts, problems, ok, notes):
    for rec in snapshot.get("skills", []):
        name = rec.get("name", "?")
        snap_path = rec.get("path", "")
        snap_sha = rec.get("sha256", "")
        snap_bytes = rec.get("bytes", 0)
        current_path = snap_path if os.path.isfile(snap_path) else _rescan_skill(name, skills_dirs)
        if not current_path:
            problems.append(_problem(
                "missing-skill-body", snap_path,
                "Skill '%s' was in the pre-compaction snapshot (%d bytes) "
                "but is gone from disk now." % (name, snap_bytes),
                "Re-create %s from the snapshot: the full pre-compaction "
                "body is stored in the snapshot file under "
                "skills[] (name '%s')." % (snap_path, name)))
            continue
        cur = file_record(current_path)
        if cur is None:
            notes.append("Could not read %s; skipped." % current_path)
            continue
        if cur["sha256"] == snap_sha:
            if cur["mtime"] > taken_ts + REATTACH_MTIME_EPSILON:
                problems.append(_problem(
                    "stale-reattach", current_path,
                    "Skill '%s' was modified after the snapshot "
                    "(mtime %s) but its current content is byte-identical "
                    "to the snapshot -- an older version appears to have "
                    "been re-attached over mid-session edits."
                    % (name, datetime.datetime.fromtimestamp(
                        cur["mtime"]).strftime("%Y-%m-%d %H:%M:%S")),
                    "Re-apply any mid-session edits you made to %s. The "
                    "snapshot only holds the pre-compaction version, so "
                    "your newer edits are not recoverable from it -- "
                    "check your editor/terminal history." % current_path))
            else:
                ok.append({"file": current_path, "state": "unchanged"})
        elif cur["bytes"] <= GUTTED_BYTES and snap_bytes > GUTTED_BYTES:
            problems.append(_problem(
                "missing-skill-body", current_path,
                "Skill '%s' body collapsed from %d bytes to %d bytes -- "
                "the body is effectively gone." % (name, snap_bytes,
                                                   cur["bytes"]),
                "Restore the body from the snapshot file (skills[], name "
                "'%s'): rewrite %s with the stored content."
                % (name, current_path)))
        else:
            ok.append({"file": current_path, "state": "changed",
                       "detail": "legitimately changed since the snapshot "
                                 "(%d -> %d bytes)" % (snap_bytes,
                                                       cur["bytes"])})


def _audit_single_file(rec, path_override, taken_ts, problems, ok, notes,
                      label, key):
    """Audit one recorded file (CLAUDE.md / MEMORY.md).

    rec: the snapshot's file record (None if the file was absent at
    snapshot time -- nothing to compare). Returns the current file record
    when the content changed, else None. Missing files become
    "missing-file" problems.
    """
    if rec is None:
        return None
    current_path = path_override or rec.get("path", "")
    if not current_path or not os.path.isfile(current_path):
        problems.append(_problem(
            "missing-file", current_path or rec.get("path", ""),
            "%s was in the pre-compaction snapshot but is no longer on "
            "disk -- everything in it is lost from the filesystem."
            % label,
            "Restore it from the snapshot file (%s[] content) or from "
            "backup." % key))
        return None
    cur = file_record(current_path)
    if cur is None:
        notes.append("Could not read %s; skipped." % current_path)
        return None
    if cur["sha256"] == rec["sha256"]:
        ok.append({"file": current_path, "state": "unchanged"})
        return None
    return cur


def _audit_claude_md(snapshot, taken_ts, problems, ok, notes,
                     summary_texts=None):
    rec = snapshot.get("claude_md")
    if rec is None:
        return
    cur = _audit_single_file(rec, None, taken_ts, problems, ok, notes,
                             "CLAUDE.md", "claude_md")
    if cur is None:
        return  # unchanged or missing (handled above)
    old_text = rec.get("content", "")
    new_text = cur.get("content", "")
    diff_text, added, removed = _diff_summary(old_text, new_text)
    if not added:
        ok.append({"file": cur["path"], "state": "changed",
                   "detail": "changed since the snapshot but no new lines "
                             "added (%d removed)" % len(removed)})
        return
    added_text = "\n".join(added)
    if summary_texts:
        newest = summary_texts[-1] if summary_texts else ""
        ratio = compact_audit.overlap_ratio(added_text, newest)
        if ratio >= compact_audit.OVERLAP_THRESHOLD:
            ok.append({"file": cur["path"], "state": "changed",
                       "detail": "new lines survived: they appear in the "
                                 "latest compaction summary (token overlap "
                                 "%.2f)" % ratio})
            return
        problems.append(_problem(
            "claude-md-new-text-lost", cur["path"],
            "CLAUDE.md gained %d new line(s) after the snapshot, but the "
            "latest compaction summary does not contain them (token "
            "overlap %.2f) -- the new text was likely dropped from the "
            "resumed context." % (len(added), ratio),
            "Paste these lines back into the session:\n" +
            "\n".join("  " + l for l in added[:40]),
            diff=diff_text, severity="lost"))
    else:
        problems.append(_problem(
            "claude-md-new-text-lost", cur["path"],
            "CLAUDE.md gained %d new line(s) after the snapshot. "
            "Compaction summaries are written from the pre-compact state, "
            "so new mid-session edits are the classic loss case."
            % len(added),
            "Verify these lines are present in your resumed context; if "
            "not, paste them back:\n" +
            "\n".join("  " + l for l in added[:40]),
            diff=diff_text, severity="check"))


def audit_against_snapshot(snapshot, skills_dirs=None, claude_md=None,
                           memory_md=None, summary_texts=None,
                           snapshot_path=""):
    """Compare current disk state against a snapshot.

    skills_dirs / claude_md / memory_md override the snapshot's recorded
    paths when given; otherwise the snapshot's own paths are reused.
    summary_texts: optional list of post-compact summary strings (oldest
    first) from the session transcript, used to cross-check CLAUDE.md new
    text. Returns a result dict with problems / ok / notes.
    """
    taken_ts = _taken_at_ts(snapshot)
    if skills_dirs is None:
        skills_dirs = snapshot.get("skills_dirs") or default_skills_dirs()
    problems, ok, notes = [], [], []
    _audit_skills(snapshot, skills_dirs, taken_ts, problems, ok, notes)
    _audit_claude_md(snapshot, taken_ts, problems, ok, notes,
                     summary_texts=summary_texts)
    if snapshot.get("memory_md") is not None:
        _audit_single_file(snapshot["memory_md"], memory_md, taken_ts,
                           problems, ok, notes, "MEMORY.md", "memory_md")
    # New skills that appeared after the snapshot: informational only.
    snap_names = {s.get("name") for s in snapshot.get("skills", [])}
    for skill in discover_skills(skills_dirs):
        if skill["name"] not in snap_names:
            notes.append("New skill '%s' appeared after the snapshot "
                         "(%s)." % (skill["name"], skill["path"]))
    return {
        "snapshot_path": snapshot_path,
        "snapshot_taken_at": snapshot.get("taken_at", "?"),
        "problems": problems,
        "ok": ok,
        "notes": notes,
    }


def latest_summaries(session_path, tool):
    """Post-compact summary texts (oldest first) from a transcript."""
    try:
        _turns, boundaries = compact_audit.find_boundaries(session_path, tool)
    except Exception:
        return []
    return [text for _idx, text in boundaries]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_CATEGORY_LABELS = {
    "missing-skill-body": "MISSING SKILL BODY",
    "stale-reattach": "STALE RE-ATTACH",
    "claude-md-new-text-lost": "CLAUDE.MD NEW TEXT LOST",
    "missing-file": "MISSING FILE",
}


def _truncate(text, limit=400):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def render_terminal(result):
    problems = result["problems"]
    out = ["session-handover snapshot audit",
           "snapshot taken: %s" % result.get("snapshot_taken_at", "?")]
    if result.get("snapshot_path"):
        out.append("snapshot file: %s" % result["snapshot_path"])
    out.append("problems: %d" % len(problems))
    for p in problems:
        label = _CATEGORY_LABELS.get(p["category"], p["category"])
        out.append("")
        out.append("[%s]%s %s" % (
            label, " (verify)" if p.get("severity") == "check" else "",
            p["file"]))
        out.append("  %s" % _truncate(p["detail"], 300))
        for line in p["suggestion"].splitlines()[:8]:
            out.append("  fix: %s" % line)
    if result["ok"]:
        out.append("")
        out.append("ok: %d file(s) unchanged or legitimately changed"
                   % len(result["ok"]))
    for note in result["notes"]:
        out.append("note: %s" % note)
    if not problems:
        out.append("No problems found: disk matches the pre-compaction "
                   "snapshot.")
    return "\n".join(out)


def render_markdown(result):
    lines = ["# Snapshot Audit", ""]
    lines.append("- **Snapshot taken:** %s"
                 % result.get("snapshot_taken_at", "?"))
    if result.get("snapshot_path"):
        lines.append("- **Snapshot file:** `%s`" % result["snapshot_path"])
    lines.append("")
    problems = result["problems"]
    if not problems:
        lines.append("No problems found: the current disk state matches the "
                     "pre-compaction snapshot.")
        lines.append("")
    else:
        lines.append("**%d problem(s) found.**" % len(problems))
        lines.append("")
        for p in problems:
            label = _CATEGORY_LABELS.get(p["category"], p["category"])
            lines.append("## %s" % label)
            lines.append("")
            if p.get("severity") == "check":
                lines.append("_Severity: needs manual verification._")
                lines.append("")
            lines.append("- **File:** `%s`" % p["file"])
            lines.append("- **Detail:** %s" % p["detail"])
            lines.append("")
            lines.append("**Suggested fix:**")
            lines.append("")
            lines.append("```")
            lines.append(p["suggestion"][:2000])
            lines.append("```")
            lines.append("")
            if p.get("diff"):
                lines.append("<details><summary>Diff (snapshot → "
                             "current)</summary>")
                lines.append("")
                lines.append("```diff")
                lines.append(p["diff"][:4000])
                lines.append("```")
                lines.append("")
                lines.append("</details>")
                lines.append("")
    if result["ok"]:
        lines.append("## Unchanged / legitimately changed")
        lines.append("")
        for entry in result["ok"]:
            extra = " -- " + entry["detail"] if entry.get("detail") else ""
            lines.append("- `%s` (%s%s)" % (entry["file"], entry["state"],
                                            extra))
        lines.append("")
    for note in result["notes"]:
        lines.append("_Note: %s_" % note)
        lines.append("")
    lines.append("---")
    lines.append("_Mechanical audit: compares the current disk against the "
                 "pre-compaction snapshot. It cannot see the resumed "
                 "context itself -- 'lost' verdicts about context should be "
                 "confirmed by skimming the session._")
    lines.append("")
    return "\n".join(lines)
