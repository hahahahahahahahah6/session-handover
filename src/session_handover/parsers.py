"""Defensive parsers for coding-agent session transcripts.

Transcript formats are UNDOCUMENTED and change over time. Every parser here
is written to degrade gracefully: malformed lines are skipped, unknown shapes
are ignored, and a parse never raises on real-world input. If a transcript
format drifts, you get less detail -- not a crash.
"""

import json
import os
import re


def _lines(path):
    """Yield parsed JSON objects from a .jsonl file, skipping bad lines."""
    try:
        fh = open(path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            if isinstance(obj, dict):
                yield obj


def _first_text(blocks):
    """Pull the first text-ish string out of a content block list."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    for b in blocks:
        if not isinstance(b, dict):
            continue
        if b.get("type") in ("text", "input_text", "output_text"):
            t = b.get("text")
            if t:
                return str(t)
    return ""


def _looks_like_error(text):
    if not text:
        return False
    t = text.lower()
    return any(k in t for k in (
        "error", "failed", "failure", "traceback", "exception",
        "not found", "enoent", "permission denied", "command not found",
    ))


# ---------------------------------------------------------------------------
# Claude Code: ~/.claude/projects/<slug>/<session-id>.jsonl
# Lines: {"type": "user"|"assistant"|"summary", "message": {...},
#         "timestamp": "...", "sessionId": "...", "cwd": "..."}
# assistant message content blocks: {"type": "text"} or
#   {"type": "tool_use", "name": "Edit"|"Write"|"Bash"|..., "input": {...}}
# ---------------------------------------------------------------------------

_CLAUDE_FILE_ACTIONS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
_CLAUDE_READ_ACTIONS = {"Read", "Glob", "Grep"}


def parse_claude_code(path):
    actions = []       # chronological notable actions
    errors = []
    first_user = ""
    user_count = 0
    assistant_count = 0
    started = ""
    cwd = ""
    session_id = os.path.splitext(os.path.basename(path))[0]
    counts = {"edits": 0, "writes": 0, "commands": 0, "reads": 0}
    files = []

    for obj in _lines(path):
        started = started or obj.get("timestamp", "")
        cwd = cwd or obj.get("cwd", "")
        session_id = obj.get("sessionId") or session_id
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", obj.get("type", ""))
        content = msg.get("content", [])

        if role == "user":
            user_count += 1
            if not first_user:
                first_user = _first_text(content)[:2000]
            # tool results may carry errors
            if isinstance(content, list):
                for b in content:
                    if isinstance(b, dict) and b.get("type") == "tool_result":
                        c = b.get("content")
                        txt = c if isinstance(c, str) else _first_text(c)
                        if b.get("is_error") or _looks_like_error(txt):
                            errors.append(txt[:500])
        elif role == "assistant":
            assistant_count += 1
            if isinstance(content, list):
                for b in content:
                    if not isinstance(b, dict):
                        continue
                    if b.get("type") == "tool_use":
                        name = b.get("name", "tool")
                        inp = b.get("input") or {}
                        if not isinstance(inp, dict):
                            inp = {}
                        if name in _CLAUDE_FILE_ACTIONS:
                            counts["edits" if name in ("Edit", "MultiEdit") else "writes"] += 1
                            p = inp.get("file_path") or inp.get("path") or ""
                            if p and p not in files:
                                files.append(p)
                            actions.append({"kind": "file", "detail": "%s %s" % (name, p or "(unknown path)")})
                        elif name == "Bash":
                            counts["commands"] += 1
                            cmd = str(inp.get("command", ""))[:160]
                            actions.append({"kind": "command", "detail": cmd or "(empty command)"})
                        elif name in _CLAUDE_READ_ACTIONS:
                            counts["reads"] += 1
                        else:
                            actions.append({"kind": "tool", "detail": name})

    return {
        "tool": "claude-code",
        "path": path,
        "session_id": session_id,
        "started": started,
        "cwd": cwd,
        "messages": user_count + assistant_count,
        "goal": first_user,
        "actions": actions,
        "errors": errors[:10],
        "files": files,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Codex CLI: ~/.codex/sessions/YYYY/MM/DD/<id>.jsonl
# Lines: {"type": "session_meta", ...},
#        {"type": "response_item", "payload": {"type": "message"|"function_call"|
#          "function_call_output"|"reasoning", ...}}
# Best-effort: the Codex CLI format has changed across versions.
# ---------------------------------------------------------------------------

def _codex_args_summary(name, arguments):
    """Summarize a function_call's arguments into a short human string."""
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return arguments[:160]
    if not isinstance(arguments, dict):
        return ""
    for key in ("command", "cmd", "path", "file", "filename", "workdir"):
        if arguments.get(key):
            return "%s %s" % (name, str(arguments[key])[:140])
    # fallback: first scalar value
    for v in arguments.values():
        if isinstance(v, (str, int, float)):
            return "%s %s" % (name, str(v)[:140])
    return name


def parse_codex(path):
    actions = []
    errors = []
    first_user = ""
    user_count = 0
    assistant_count = 0
    started = ""
    cwd = ""
    session_id = os.path.splitext(os.path.basename(path))[0]
    counts = {"edits": 0, "writes": 0, "commands": 0, "reads": 0}
    files = []

    for obj in _lines(path):
        otype = obj.get("type", "")
        if otype == "session_meta":
            meta = obj.get("payload") or {}
            started = started or obj.get("timestamp", "") or meta.get("timestamp", "")
            cwd = cwd or meta.get("cwd", "") or obj.get("cwd", "")
            session_id = meta.get("id") or session_id
            continue
        if otype != "response_item":
            continue
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = p.get("type", "")
        started = started or obj.get("timestamp", "")

        if ptype == "message":
            role = p.get("role", "")
            text = _first_text(p.get("content", []))
            if role == "user":
                user_count += 1
                if not first_user:
                    first_user = text[:2000]
                if _looks_like_error(text):
                    errors.append(text[:500])
            elif role == "assistant":
                assistant_count += 1
        elif ptype == "function_call":
            name = p.get("name", "tool")
            summary = _codex_args_summary(name, p.get("arguments", ""))
            nl = name.lower()
            if any(k in nl for k in ("patch", "edit", "write", "apply")):
                counts["edits"] += 1
                m = re.search(r"[\w\-./]+\.\w+", summary)
                if m and m.group(0) not in files:
                    files.append(m.group(0))
                actions.append({"kind": "file", "detail": summary or name})
            elif any(k in nl for k in ("shell", "exec", "bash", "command", "run")):
                counts["commands"] += 1
                actions.append({"kind": "command", "detail": summary or name})
            elif any(k in nl for k in ("read", "view", "cat", "glob", "grep")):
                counts["reads"] += 1
            else:
                actions.append({"kind": "tool", "detail": summary or name})
        elif ptype == "function_call_output":
            out = p.get("output")
            txt = out if isinstance(out, str) else str(out or "")[:500]
            status = str(p.get("status", "")).lower()
            if status not in ("", "success", "completed", "ok") or _looks_like_error(txt):
                errors.append(txt[:500])
        # "reasoning" and anything else: intentionally ignored

    return {
        "tool": "codex",
        "path": path,
        "session_id": session_id,
        "started": started,
        "cwd": cwd,
        "messages": user_count + assistant_count,
        "goal": first_user,
        "actions": actions,
        "errors": errors[:10],
        "files": files,
        "counts": counts,
    }


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def claude_dir():
    return os.environ.get(
        "SESSION_HANDOVER_CLAUDE_DIR",
        os.path.expanduser("~/.claude/projects"))


def codex_dir():
    return os.environ.get(
        "SESSION_HANDOVER_CODEX_DIR",
        os.path.expanduser("~/.codex/sessions"))


def _iter_jsonl(root):
    if not os.path.isdir(root):
        return
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            if fn.endswith(".jsonl"):
                yield os.path.join(dirpath, fn)


def discover():
    """Return list of parsed session dicts, newest first. Never raises."""
    sessions = []
    for path in _iter_jsonl(claude_dir()):
        try:
            sessions.append(parse_claude_code(path))
        except Exception:
            continue
    for path in _iter_jsonl(codex_dir()):
        try:
            sessions.append(parse_codex(path))
        except Exception:
            continue

    def sort_key(s):
        try:
            return os.path.getmtime(s["path"])
        except OSError:
            return 0

    sessions.sort(key=sort_key, reverse=True)
    return sessions


def find_session(sessions, ident):
    """Match by full or prefix of session_id."""
    ident = ident.strip()
    for s in sessions:
        if s["session_id"] == ident or s["session_id"].startswith(ident):
            return s
    return None
