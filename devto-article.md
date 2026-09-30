# I use Claude Code and Codex side by side. Handing off between them was pure pain, so I built a CLI

I split my coding work across two agents: Claude Code for most things, Codex CLI when I want a second opinion or a different model. The problem hits every time I switch mid-task: the new session knows nothing. I'd open a blank `handover.md` and type from memory what the last session did — which files it touched, what broke, where I left off. Half the time I'd forget the exact error message, and the receiving agent would redo work or repeat the same failed command.

There are good handoff tools for Claude Code alone (Matt Pocock's `/handoff` skill, for example). But nobody was doing the cross-tool case: one command that reads *both* tools' local session transcripts and spits out a handover doc the next agent — whichever agent — can pick up.

So I built one. It's called `session-handover`.

## What it does

Both CLIs already keep everything on disk. Claude Code writes each session as `.jsonl` under `~/.claude/projects`; Codex CLI writes per-day `.jsonl` files under `~/.codex/sessions`. The CLI walks both:

```bash
pip install session-handover

$ session-handover list
TOOL         SESSION                  STARTED           MESSAGES
codex        0199f3a1-…               2026-09-30 00:41   57
claude-code  a4c2e901-…               2026-09-29 23:58   34

$ session-handover make --session a4c2e901 --out HANDOVER.md
Wrote HANDOVER.md (claude-code / a4c2e901-…)
```

The generated document has the shape I was hand-writing anyway:

- **Goal** — the session's first user message
- **What happened** — counts plus a timeline: which files were edited, which commands were run
- **Files touched**, plus `git diff --stat` when the session's working dir is a repo
- **Errors encountered** — failed commands and tracebacks, quoted verbatim
- **Open items** and a **next steps** checklist for the receiving agent

No LLM in the loop. It's heuristic extraction over the transcript — fast, local, private, zero dependencies (stdlib only).

## The annoying part: undocumented formats

Neither transcript format is documented, and both drift. Codex's in particular has changed shape across versions. So the parsers are written to degrade, not crash: every line is parsed defensively, unknown shapes are skipped, malformed lines are dropped, and a corrupt file just yields less detail instead of killing the whole `list` run. The Codex side is explicitly best-effort — when its format moves again, you lose detail, not the tool.

## Honest limitations

- Heuristic, not intelligent: it summarizes what the transcript *says*, it doesn't understand *why* decisions were made. Read the handover before trusting it.
- `--last` orders by file mtime; clock skew can misorder the list.
- Local transcripts only — nothing leaves your machine.

## Links

- GitHub: https://github.com/hahahahahahahahah6/session-handover (MIT)
- PyPI: `pip install session-handover`

11 smoke tests pass, including fake fixtures for both transcript formats and a malformed-input test that asserts the parser never crashes on garbage.

If you also bounce between agents: what does your handover ritual look like? I'm curious what I missed.
