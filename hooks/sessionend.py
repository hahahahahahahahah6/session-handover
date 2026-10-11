#!/usr/bin/env python3
"""session-handover SessionEnd hook (single-file, stdlib-only).

Registered by `session-handover hook-install sessionend`. Reads the
SessionEnd hook JSON from stdin (session_id, transcript_path), writes a
lightweight handoff (unfinished tasks, key decisions, follow-ups) to
~/.cache/session-handover/sessionend/.

Fail-open: any failure prints a stderr note and exits 0. This hook must
NEVER block anything.

Works two ways:
  1. Repo checkout: ../src is added to sys.path so the sibling package is
     used.
  2. pip install: falls back to the installed session_handover package.
"""

import os
import sys


def main():
    repo_src = os.path.abspath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "..", "src"))
    if os.path.isdir(os.path.join(repo_src, "session_handover")):
        sys.path.insert(0, repo_src)
    try:
        from session_handover.sessionend import sessionend_main
    except Exception as e:
        sys.stderr.write("session-handover sessionend hook skipped "
                         "(cannot import session_handover): %s\n" % e)
        sys.exit(0)
    sys.exit(sessionend_main())


if __name__ == "__main__":
    main()
