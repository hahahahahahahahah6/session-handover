"""session-handover: hand off a coding-agent session to the next one.

Reads local session transcripts from Claude Code (~/.claude/projects) and
Codex CLI (~/.codex/sessions) and generates a structured Markdown handover.
"""

import argparse
import datetime
import os
import sys

from . import parsers
from . import compact_audit
from .handover import generate


def _fmt_time(s):
    started = s.get("started", "")
    if started:
        # ISO-ish timestamps: keep date + time, drop the rest
        return started[:16].replace("T", " ")
    try:
        ts = os.path.getmtime(s["path"])
        return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except OSError:
        return "?"


def cmd_list(args):
    sessions = parsers.discover(fast=True)
    if not sessions:
        print("No sessions found.")
        print("Looked in:")
        print("  " + parsers.claude_dir())
        print("  " + parsers.codex_dir())
        return 0
    print("%-12s %-24s %-17s %s" % ("TOOL", "SESSION", "STARTED", "MESSAGES"))
    for s in sessions[: args.limit]:
        print("%-12s %-24s %-17s %d" % (
            s["tool"], s["session_id"][:24], _fmt_time(s), s["messages"]))
    return 0


def cmd_make(args):
    sessions = parsers.discover()
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return 1
    if args.session:
        s = parsers.find_session(sessions, args.session)
        if not s:
            print("No session matching %r." % args.session, file=sys.stderr)
            return 1
    else:
        s = sessions[0]  # --last is the default
    text = generate(s)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text)
        print("Wrote %s (%s / %s)" % (args.out, s["tool"], s["session_id"]))
    else:
        print(text)
    return 0


def cmd_audit_compact(args):
    sessions = parsers.discover()
    if not sessions:
        print("No sessions found.", file=sys.stderr)
        return 1
    if args.session:
        s = parsers.find_session(sessions, args.session)
        if not s:
            print("No session matching %r." % args.session, file=sys.stderr)
            return 1
    else:
        s = sessions[0]  # --last is the default
    result = compact_audit.audit(s["path"], s["tool"])
    text = compact_audit.render_terminal(result, s["session_id"])
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(compact_audit.render_markdown(result,
                                                   s["session_id"]))
        print("Wrote %s (%s / %s)" % (args.out, s["tool"],
                                      s["session_id"]))
    else:
        print(text)
    if args.fail_on_drop and any(b["dropped"] for b in result["boundaries"]):
        return 1
    return 0


def build_parser():
    p = argparse.ArgumentParser(
        prog="session-handover",
        description="Generate handover docs from Claude Code / Codex CLI session transcripts.")
    sub = p.add_subparsers(dest="cmd", required=True)

    pl = sub.add_parser("list", help="List recent local sessions.")
    pl.add_argument("--limit", type=int, default=20)
    pl.set_defaults(func=cmd_list)

    pm = sub.add_parser("make", help="Generate a handover document.")
    pm.add_argument("--last", action="store_true",
                    help="Use the most recent session (default).")
    pm.add_argument("--session", metavar="ID",
                    help="Use the session whose id matches (prefix ok).")
    pm.add_argument("--out", metavar="HANDOVER.md",
                    help="Write to file instead of stdout.")
    pm.set_defaults(func=cmd_make)

    pa = sub.add_parser("audit-compact",
                        help="Audit what /compact dropped: extract durable "
                             "items from pre-compact turns and check them "
                             "against the compaction summary.")
    pa.add_argument("--last", action="store_true",
                    help="Use the most recent session (default).")
    pa.add_argument("--session", metavar="ID",
                    help="Use the session whose id matches (prefix ok).")
    pa.add_argument("--out", metavar="AUDIT.md",
                    help="Write the Markdown report to file instead of "
                         "printing the terminal summary.")
    pa.add_argument("--fail-on-drop", action="store_true",
                    help="Exit 1 when any item was dropped (for CI/hook "
                         "use). Default is exit 0: audit, never blocks.")
    pa.set_defaults(func=cmd_audit_compact)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
