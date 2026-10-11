"""SessionEnd hook: write a lightweight handoff when a session ends.

``session-handover hook --session-end`` is meant to be called from a
Claude Code SessionEnd hook. It reads the hook JSON from stdin
(``session_id``, ``transcript_path``), extracts the session's unfinished
tasks, key decisions, and follow-ups from the transcript tail, and writes
a small handoff file (JSON or Markdown) to
``~/.cache/session-handover/sessionend/`` (override with
``SESSION_HANDOVER_SESSIONEND_DIR``).

Fail-open: any failure prints a stderr note and exits 0. It never blocks
anything. Extraction is heuristic (see compact_audit) and capped to the
most recent SESSIONEND_WINDOW text turns; no LLM, no network, stdlib only.
"""

import datetime
import json
import os
import shlex
import sys

from . import compact_audit
from . import parsers
from . import precompact

# How many recent text-bearing turns feed the extraction. Kept small on
# purpose: this is a lightweight handoff, not a full transcript summary.
SESSIONEND_WINDOW = int(os.environ.get("SESSION_HANDOVER_SESSIONEND_WINDOW",
                                       "200"))

# Max items per section in the handoff file.
SECTION_CAP = 25

OUT_DIR_ENV = "SESSION_HANDOVER_SESSIONEND_DIR"


def output_dir():
    custom = os.environ.get(OUT_DIR_ENV)
    if custom:
        return custom
    return os.path.join(os.path.expanduser("~"), ".cache",
                        "session-handover", "sessionend")


def safe_session_id(session_id):
    """Sanitize a session id for use in a filename (no path traversal)."""
    import re
    s = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id or "")).strip("._")[:64]
    return s or "unknown"


def parse_sessionend_input(text):
    """Tolerantly parse SessionEnd hook stdin.

    Returns {"session_id", "transcript_path"} (either may be missing).
    Non-JSON or non-object input yields {} -- the caller treats that as
    "fall back to the newest local session", never an error.
    """
    try:
        obj = json.loads(text or "")
    except Exception:
        return {}
    if not isinstance(obj, dict):
        return {}
    session_id = obj.get("session_id") or obj.get("sessionId") or ""
    transcript = (obj.get("transcript_path") or obj.get("transcriptPath")
                  or "")
    return {"session_id": str(session_id), "transcript_path": str(transcript)}


def extract_handoff(transcript_path, tool):
    """Extract goal + unfinished tasks + decisions + follow-ups.

    Buckets come from compact_audit's heuristic item kinds: TODOs become
    "unfinished tasks", decisions become "key decisions", and
    rules/preferences become "follow-ups & constraints" (things the next
    session should keep in mind). All heuristic -- documented in the
    output file itself.
    """
    try:
        turns, _boundaries = compact_audit.find_boundaries(transcript_path,
                                                           tool)
    except Exception:
        turns = []
    recent = turns[-SESSIONEND_WINDOW:]
    try:
        items = compact_audit.extract_items(recent)
    except Exception:
        items = []
    unfinished, decisions, follow_up = [], [], []
    for item in items:
        entry = {"kind": item["kind"], "text": item["text"],
                 "turn": item["turn"]}
        if item["kind"] == "todo":
            unfinished.append(entry)
        elif item["kind"] == "decision":
            decisions.append(entry)
        else:
            follow_up.append(entry)
    goal = turns[0][1] if turns else ""
    return {
        "goal": " ".join(goal.split())[:500],
        "unfinished": unfinished[:SECTION_CAP],
        "decisions": decisions[:SECTION_CAP],
        "follow_up": follow_up[:SECTION_CAP],
        "turns_scanned": len(recent),
    }


def build_handoff(session_id, transcript_path, tool):
    """Assemble the handoff dict (JSON-serializable)."""
    data = extract_handoff(transcript_path, tool)
    return {
        "format": "session-handover-sessionend",
        "format_version": 1,
        "session_id": session_id,
        "ended_at": datetime.datetime.now(
            datetime.timezone.utc).isoformat(),
        "transcript": transcript_path,
        "goal": data["goal"],
        "unfinished_tasks": data["unfinished"],
        "key_decisions": data["decisions"],
        "follow_ups": data["follow_up"],
        "turns_scanned": data["turns_scanned"],
    }


def _short_err(e):
    return " ".join(str(e).split())[:200]


def _default_out_path(session_id, fmt):
    sid = safe_session_id(session_id)
    ext = "md" if fmt == "markdown" else "json"
    return os.path.join(output_dir(), "HANDOFF.%s.%s" % (sid, ext))


def render_markdown(handoff):
    lines = ["# Session Handoff", ""]
    lines.append("- **Session:** `%s`" % handoff.get("session_id", "?"))
    lines.append("- **Ended:** %s" % handoff.get("ended_at", "?"))
    lines.append("")
    if handoff.get("goal"):
        lines.append("## Goal")
        lines.append("")
        lines.append(handoff["goal"])
        lines.append("")

    def section(title, items, checkbox):
        lines.append("## %s" % title)
        lines.append("")
        if not items:
            lines.append("_None detected in the recent turns._")
        else:
            for it in items:
                mark = "- [ ] " if checkbox else "- "
                lines.append("%s**[%s]** (turn %d) %s" %
                             (mark, it["kind"], it["turn"], it["text"]))
        lines.append("")

    section("Unfinished tasks", handoff.get("unfinished_tasks"), True)
    section("Key decisions", handoff.get("key_decisions"), False)
    section("Follow-ups & constraints", handoff.get("follow_ups"), True)
    lines.append("---")
    lines.append("_Heuristic handoff: extracted from the last %d text turn(s) "
                 "with token-pattern matching (no LLM). Subtly-phrased items "
                 "may be missing -- skim before trusting._"
                 % handoff.get("turns_scanned", 0))
    lines.append("")
    return "\n".join(lines)


def run_sessionend(session_id, transcript_path, out=None, fmt="json"):
    """Write the handoff file. Returns the path written.

    Never raises: on transcript parse failure a degraded handoff (empty
    sections, with a note) is still written when possible.
    """
    sid = safe_session_id(session_id)
    if out and os.path.isdir(out):
        out_path = _default_out_path(sid, fmt)
        out_path = os.path.join(out, os.path.basename(out_path))
    else:
        out_path = out or _default_out_path(sid, fmt)
    os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)

    tool = "claude-code"
    try:
        tool = parsers.peek(transcript_path).get("tool") or "claude-code"
    except Exception:
        pass
    try:
        handoff = build_handoff(sid, transcript_path, tool)
    except Exception as e:  # degraded handoff, still written
        handoff = {
            "format": "session-handover-sessionend",
            "format_version": 1,
            "session_id": sid,
            "ended_at": datetime.datetime.now(
                datetime.timezone.utc).isoformat(),
            "transcript": transcript_path,
            "goal": "",
            "unfinished_tasks": [],
            "key_decisions": [],
            "follow_ups": [],
            "turns_scanned": 0,
            "note": "transcript parse failed (%s)" % _short_err(e),
        }
    if fmt == "markdown":
        text = render_markdown(handoff)
    else:
        text = json.dumps(handoff, indent=2, ensure_ascii=False) + "\n"
    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return out_path


def _fallback_session():
    """Newest local session, for when hook stdin carries nothing usable."""
    try:
        sessions = parsers.discover()
    except Exception:
        return None
    return sessions[0] if sessions else None


def sessionend_main(stdin_text=None, out=None, fmt="json"):
    """Hook body. Always returns 0 -- fail-open, never blocks anything."""
    try:
        if stdin_text is None:
            stdin_text = sys.stdin.read()
        info = parse_sessionend_input(stdin_text)
        session_id = info.get("session_id", "")
        transcript_path = info.get("transcript_path", "")
        if not transcript_path or not os.path.isfile(transcript_path):
            fb = _fallback_session()
            if fb is None:
                sys.stderr.write("session-handover sessionend: no "
                                 "transcript available; skipping.\n")
                return 0
            session_id = session_id or fb["session_id"]
            transcript_path = fb["path"]
        if not session_id:
            session_id = os.path.splitext(
                os.path.basename(transcript_path))[0]
        path = run_sessionend(session_id, transcript_path, out=out, fmt=fmt)
        sys.stderr.write("session-handover sessionend: wrote %s\n" % path)
        return 0
    except Exception as e:  # fail-open
        sys.stderr.write("session-handover sessionend hook skipped: %s\n"
                         % _short_err(e))
        return 0


# ---------------------------------------------------------------------------
# Hook installation into ~/.claude/settings.json (SessionEnd event)
# ---------------------------------------------------------------------------

def settings_path():
    return precompact.settings_path()


def _hook_command():
    return "%s %s" % (shlex.quote(sys.executable),
                      shlex.quote(_shim_path()))


def _package_dir():
    return os.path.dirname(os.path.abspath(__file__))


def _shim_path():
    """Absolute path of hooks/sessionend.py to register.

    Same two-mode scheme as the PreCompact hook: a repo checkout registers
    hooks/sessionend.py next to src/; otherwise a generated shim is
    materialized into ~/.cache/session-handover/hooks/sessionend.py.
    """
    repo_shim = os.path.abspath(os.path.join(_package_dir(), "..", "..",
                                             "hooks", "sessionend.py"))
    if os.path.isfile(repo_shim):
        return repo_shim
    dest_dir = os.path.join(os.path.expanduser("~"), ".cache",
                            "session-handover", "hooks")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "sessionend.py")
    if not os.path.isfile(dest):
        with open(dest, "w", encoding="utf-8") as fh:
            fh.write(GENERATED_SHIM)
    return dest


GENERATED_SHIM = '''#!/usr/bin/env python3
"""session-handover SessionEnd hook shim (generated by `hook-install sessionend`).

Fail-open: never blocks anything. Delegates to the installed
session_handover package; if that import fails, exits 0 with a note.
"""

import sys


def main():
    try:
        from session_handover.sessionend import sessionend_main
        sessionend_main()
    except Exception as e:
        sys.stderr.write("session-handover sessionend hook skipped: %s\\n" % e)
    sys.exit(0)


if __name__ == "__main__":
    main()
'''


def sessionend_install():
    """Register the SessionEnd hook in ~/.claude/settings.json.

    Merges with existing settings; never clobbers other hooks or events.
    Idempotent. Returns the command string registered.
    """
    command = _hook_command()
    path = settings_path()
    data = precompact._load_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
        data["hooks"] = hooks
    entries = hooks.get("SessionEnd")
    if not isinstance(entries, list):
        entries = []
        hooks["SessionEnd"] = entries

    for entry in entries:
        nested = entry.get("hooks") if isinstance(entry, dict) else None
        if isinstance(nested, list):
            for h in nested:
                if isinstance(h, dict) and h.get("command") == command:
                    return command  # already installed

    entries.append({"hooks": [{"type": "command", "command": command}]})
    precompact._save_settings(path, data)
    return command


def sessionend_uninstall():
    """Remove our SessionEnd entry. Idempotent. Returns True if removed."""
    command = _hook_command()
    path = settings_path()
    data = precompact._load_settings(path)
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        return False
    entries = hooks.get("SessionEnd")
    if not isinstance(entries, list):
        return False
    removed = False
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            kept.append(entry)
            continue
        nested = entry.get("hooks")
        if not isinstance(nested, list):
            kept.append(entry)
            continue
        remaining = [h for h in nested
                     if not (isinstance(h, dict)
                             and h.get("command") == command)]
        if len(remaining) != len(nested):
            removed = True
        if remaining:
            entry["hooks"] = remaining
            kept.append(entry)
    if kept:
        hooks["SessionEnd"] = kept
    else:
        del hooks["SessionEnd"]
    if removed:
        precompact._save_settings(path, data)
    return removed
