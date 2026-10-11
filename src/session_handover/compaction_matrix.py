"""Cross-harness compaction semantics matrix (v0.5).

v0.2/v0.4 audit what Claude Code's /compact dropped, against the
transcript. This module widens the lens: every coding harness compacts
differently -- what one drops, another keeps. It is a documented registry
of each mainstream harness's compaction/compression semantics:

  - what triggers it (manual command, automatic threshold)
  - what mechanism it uses (model-written summary, encrypted blob, prune)
  - what survives and what is dropped
  - the durable anchor: where to pin state so it survives
  - whether session-handover can audit it from a local transcript

Entries are grounded in measured evidence or vendor/community docs where
available (see "evidence"), and labeled honestly otherwise ("confidence").

No LLM, stdlib only. This module is pure data + rendering; the audit
engine (compact_audit) is reused for the two parseable harnesses.
"""

# audit levels:
#   full    - transcript has reliable compaction markers; audit-compact
#             detects boundaries and diffs durable items (claude-code)
#   partial - best-effort marker detection (codex-cli checkpoint messages)
#   none    - no supported local transcript; matrix is documentation only
AUDIT_LEVELS = ("full", "partial", "none")

# confidence levels:
#   measured   - someone ran repeated compactions and counted (rulestack)
#   documented - vendor docs / changelog / code
#   community  - widely reported behavior, not formally verified
CONFIDENCE_LEVELS = ("measured", "documented", "community")

_HARNESSES = [
    {
        "id": "claude-code",
        "name": "Claude Code",
        "trigger": "manual /compact; auto-compact near the context limit",
        "mechanism": ("Model-written summary replaces the history. System "
                      "prompt and output style are never part of the history. "
                      "Project-root CLAUDE.md, unscoped rules, and auto "
                      "memory (MEMORY.md) are re-injected from disk; invoked "
                      "skill bodies are re-injected capped at 5,000 tokens "
                      "per skill / 25,000 total, oldest dropped first. "
                      "Compact hook output is attached to the boundary."),
        "survives": [
            "project-root CLAUDE.md (re-read from disk, 8 of 8 compactions)",
            "unscoped rules and @import files (with the root file)",
            "MEMORY.md auto memory (re-injected from disk)",
            "invoked skill bodies (6 of 8 compactions; re-attached)",
            "passphrase from chat (as text in the summary, 8 of 8)",
            "explicit requests, goal, files modified, major decisions",
        ],
        "drops": [
            "path-scoped rules (lost until a matching file is read again; "
            "2 of 4 compactions)",
            "nested CLAUDE.md (lost until a file there is read; 4 of 8)",
            "skill body updates: an edited skill re-attaches the OLD text "
            "(2 of 8 in the measured set)",
            "specific early instructions, minor caveats, rejected-approach "
            "reasoning (folded into the summary)",
        ],
        "anchor": "Project-root CLAUDE.md -- re-injected after every "
                  "compaction. Rules that must survive belong there, not "
                  "buried in message 3 of a 200-message thread.",
        "markers": ["system compact_boundary", "isCompactSummary",
                    "continued from a previous conversation"],
        "audit": "full",
        "confidence": "measured",
        "evidence": ("rulestack, 'What survives /compact in Claude Code': "
                     "8 measured compactions (root CLAUDE.md 8/8, skill body "
                     "6/8 with stale text after edit, nested CLAUDE.md 4/8, "
                     "path-scoped rules 2/4). claudefa.st 'What Survives "
                     "/compact' survival table (skill re-injection caps, "
                     "MEMORY.md re-injection)."),
    },
    {
        "id": "codex-cli",
        "name": "Codex CLI",
        "trigger": "manual /compact; automatic pre-turn and mid-turn "
                   "compaction under token pressure",
        "mechanism": ("Remote v2 compaction: server returns an encrypted "
                      "compaction blob plus retained user messages; the blob "
                      "is not a readable summary. Local fallback (non-OpenAI "
                      "providers): a normal turn with a fixed 'CONTEXT "
                      "CHECKPOINT COMPACTION' prompt produces a handoff "
                      "summary that becomes a user message ('another "
                      "language model started to solve this problem...'), "
                      "while retained user messages are kept. Reasoning "
                      "items are filtered before summarization; the "
                      "compaction prompt is user-configurable since v0.50."),
        "survives": [
            "retained user messages (selected user turns survive)",
            "fresh canonical context re-injected at the next turn "
            "(instructions, personality, skills)",
            "ghost snapshots: internal state receipts kept as breadcrumbs "
            "(not sent to the model)",
            "the checkpoint summary itself (paraphrase of prior work)",
        ],
        "drops": [
            "assistant messages (local fallback drops all assistant "
            "messages before the boundary)",
            "reasoning items (filtered before the compaction call)",
            "verbatim phrasing of earlier turns (summary is a paraphrase)",
            "stale developer/context items (dropped in the rebuild)",
        ],
        "anchor": "Custom compact_prompt (config file, v0.50+) -- tell the "
                  "compaction what to keep: completed steps with file "
                  "paths, outstanding TODOs, decisions and constraints. "
                  "AGENTS.md is re-injected as developer instructions.",
        "markers": ["**CONTEXT CHECKPOINT - <date>** assistant message",
                    "'another language model started to solve' user message",
                    "type=compaction response item"],
        "audit": "partial",
        "confidence": "documented",
        "evidence": ("osolmaz gist: remote compaction v2 over 1087 real "
                     "compactions (encrypted blob, retained-message "
                     "conventions). openai/codex discussion #5799: "
                     "configurable compact_prompt (v0.50), reasoning "
                     "filtering, ghost snapshots. Community observation: "
                     "local fallback drops assistant messages, summary "
                     "becomes a user message."),
    },
    {
        "id": "gemini-cli",
        "name": "Gemini CLI",
        "trigger": "manual /compress; automatic under context pressure",
        "mechanism": ("Model-written summary replaces older history. "
                      "A PreCompress hook fires before compression but is "
                      "advisory only: it cannot block compression and runs "
                      "asynchronously, so it is a best-effort preservation "
                      "window, not a guaranteed save point."),
        "survives": [
            "recent tail of the conversation",
            "the compression summary (model's paraphrase)",
            "GEMINI.md re-read at startup/resume",
        ],
        "drops": [
            "verbatim earlier turns and exact tool outputs",
            "anything the summary paraphrased away",
            "PreCompress hook output is best-effort only (advisory)",
        ],
        "anchor": "GEMINI.md -- re-read at startup and resume. "
                  "session-handover's snapshot command can freeze state "
                  "before /compress since the hook cannot be relied on.",
        "markers": [],
        "audit": "none",
        "confidence": "community",
        "evidence": ("Community reports; hook-mapping docs list a "
                     "PreCompress hook as advisory-only (cannot block, "
                     "async). No local transcript audit support yet."),
    },
    {
        "id": "aider",
        "name": "Aider",
        "trigger": "automatic summarization toward the token budget; "
                   "/clear-chat wipes the chat",
        "mechanism": ("Chat history is summarized to fit the model's token "
                      "budget; repo map and file state are re-derived from "
                      "disk. /clear-chat is a hard reset, not a compaction."),
        "survives": [
            "committed files on disk (the ground truth aider re-derives)",
            "repo map rebuilt from the working tree",
            "the summarizer's paraphrase of the session",
        ],
        "drops": [
            "verbatim chat turns beyond the budget",
            "uncommitted reasoning about rejected approaches",
            "everything, on /clear-chat",
        ],
        "anchor": "Commit before compress. Durable decisions belong in "
                  "committed project files (repo conventions), not in "
                  "chat turns that only the summarizer remembers.",
        "markers": [],
        "audit": "none",
        "confidence": "community",
        "evidence": ("Community reports of summarization-based compression; "
                     "aider re-derives state from disk. No local transcript "
                     "audit support yet."),
    },
    {
        "id": "opencode",
        "name": "OpenCode",
        "trigger": "manual /compact (and orchestrated compaction via CLI "
                   "capability rows)",
        "mechanism": ("Session compaction summarization; sessions persist "
                      "in a local SQLite store (~/.local/share/opencode) "
                      "which can be queried read-only."),
        "survives": [
            "the compaction summary",
            "AGENTS.md loaded per session",
            "full pre-compaction session rows in the SQLite store",
        ],
        "drops": [
            "verbatim earlier turns (folded into the summary)",
        ],
        "anchor": "AGENTS.md -- loaded per session. The SQLite store keeps "
                  "pre-compaction rows, so a lost detail can be recovered "
                  "by querying history.",
        "markers": [],
        "audit": "none",
        "confidence": "community",
        "evidence": ("CLI capability tables list a compact command for "
                     "opencode; community tools query the SQLite session "
                     "store read-only. No transcript audit support yet."),
    },
    {
        "id": "copilot-cli",
        "name": "GitHub Copilot CLI",
        "trigger": "manual /compact; auto-compaction around ~95% of the "
                   "token limit",
        "mechanism": ("Background history compression, unprompted at the "
                      "threshold; manual /compact on demand. /context shows "
                      "a token-usage breakdown of what is eating the "
                      "window."),
        "survives": [
            "copilot-instructions.md / AGENTS.md / CLAUDE.md re-read",
            "the compression summary",
            "recent turns",
        ],
        "drops": [
            "verbatim earlier turns and exact outputs",
            "un-summarized early instructions",
        ],
        "anchor": ".github/copilot-instructions.md (or AGENTS.md) -- "
                  "re-read into context; pin must-survive rules there.",
        "markers": [],
        "audit": "none",
        "confidence": "documented",
        "evidence": ("Copilot CLI changelog adds /compact, /context, "
                     "/usage commands; product writeups document "
                     "auto-compaction at ~95% and background compression."),
    },
    {
        "id": "cursor",
        "name": "Cursor Agent",
        "trigger": "automatic summarization of long agent chats",
        "mechanism": ("Long agent conversations are summarized in the "
                      "background; project rules are re-applied from disk."),
        "survives": [
            ".cursor/rules project rules (re-applied)",
            "recent conversation tail",
            "the auto-summary's paraphrase",
        ],
        "drops": [
            "verbatim earlier turns",
            "exact tool outputs and file snapshots",
        ],
        "anchor": ".cursor/rules -- version-controlled project rules that "
                  "are re-applied after summarization.",
        "markers": [],
        "audit": "none",
        "confidence": "community",
        "evidence": ("Community reports of background summarization in "
                     "long agent chats. No local transcript audit support "
                     "yet."),
    },
    {
        "id": "cline",
        "name": "Cline",
        "trigger": "manual condense; automatic context management",
        "mechanism": ("Context condense summarizes the task history; "
                      ".clinerules are re-read per task."),
        "survives": [
            ".clinerules re-read per task",
            "the condense summary",
            "recent task state",
        ],
        "drops": [
            "verbatim earlier task turns",
            "exact tool results",
        ],
        "anchor": ".clinerules -- re-read per task; pin durable "
                  "constraints there.",
        "markers": [],
        "audit": "none",
        "confidence": "community",
        "evidence": ("Community reports of condense summarization. No "
                     "local transcript audit support yet."),
    },
]


def harnesses():
    """Return harness ids in display order."""
    return [h["id"] for h in _HARNESSES]


def get(harness_id):
    """Return the semantics entry for a harness id.

    Raises ValueError with the known ids on unknown input -- fail loud
    here, because a silently-unknown harness would poison comparisons.
    """
    for h in _HARNESSES:
        if h["id"] == harness_id:
            return h
    raise ValueError("unknown harness %r (known: %s)"
                     % (harness_id, ", ".join(harnesses())))


def compare(a_id, b_id):
    """Compare two harnesses' compaction semantics.

    Returns a dict with drops/survives unique to each side and shared,
    matched on normalized text (case-insensitive substring containment),
    plus the two anchors. Token-level, heuristic -- same spirit as the
    audit's token-overlap matching, documented here.
    """
    a, b = get(a_id), get(b_id)

    def _norm(items):
        return [" ".join(x.lower().split()) for x in items]

    def _unique(items, others):
        out = []
        for raw, n in zip(items, _norm(items)):
            # "same" if either side contains the other as a substring
            if not any(n in o or o in n for o in _norm(others)):
                out.append(raw)
        return out

    return {
        "a": a_id, "b": b_id,
        "drops_only_in_a": _unique(a["drops"], b["drops"]),
        "drops_only_in_b": _unique(b["drops"], a["drops"]),
        "survives_only_in_a": _unique(a["survives"], b["survives"]),
        "survives_only_in_b": _unique(b["survives"], a["survives"]),
        "anchor_a": a["anchor"], "anchor_b": b["anchor"],
        "audit_a": a["audit"], "audit_b": b["audit"],
    }


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def render_terminal():
    """Compact cross-harness comparison table for the terminal."""
    rows = [("HARNESS", "TRIGGER", "MECHANISM", "AUDIT")]
    for h in _HARNESSES:
        mech = h["mechanism"]
        mech = mech if len(mech) <= 64 else mech[:61] + "..."
        trig = h["trigger"]
        trig = trig if len(trig) <= 40 else trig[:37] + "..."
        rows.append((h["id"], trig, mech, h["audit"]))
    widths = [max(len(r[i]) for r in rows) for i in range(4)]
    out = []
    for i, r in enumerate(rows):
        out.append("  ".join(c.ljust(w) for c, w in zip(r, widths)).rstrip())
        if i == 0:
            out.append("  ".join("-" * w for w in widths))
    out.append("")
    out.append("audit: full = audit-compact diffs this harness from its "
               "transcript;")
    out.append("       partial = best-effort checkpoint markers; "
               "none = documentation only.")
    out.append("Run `session-handover compact-matrix --out MATRIX.md` for "
               "the full report.")
    return "\n".join(out)


def _truncate(text, limit=200):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def render_markdown(compare_pair=None):
    """Full Markdown report: comparison table + per-harness sections.

    compare_pair: optional (a_id, b_id) tuple to append a pairwise diff.
    """
    lines = ["# Compaction Semantics Matrix", "",
             "What each coding harness drops and keeps when it compacts -- "
             "so you know what to pin in a durable anchor before it happens.",
             ""]
    # Comparison table
    lines += ["| Harness | Trigger | What survives | What drops | Anchor | "
              "Transcript audit |",
              "| --- | --- | --- | --- | --- | --- |"]
    for h in _HARNESSES:
        lines.append("| %s | %s | %s | %s | %s | %s |" % (
            h["name"],
            _truncate(h["trigger"], 90),
            _truncate("; ".join(h["survives"]), 160),
            _truncate("; ".join(h["drops"]), 160),
            _truncate(h["anchor"], 120),
            h["audit"]))
    lines += ["", "_audit_: full = `audit-compact` diffs durable items from "
              "the transcript; partial = best-effort checkpoint markers; "
              "none = documentation only (no supported local transcript)._",
              ""]
    # Per-harness detail
    for h in _HARNESSES:
        lines += ["## %s" % h["name"], "",
                  "- **Trigger:** %s" % h["trigger"], "",
                  "- **Mechanism:** %s" % h["mechanism"], "",
                  "- **Survives:**", ""]
        lines += ["  - %s" % s for s in h["survives"]] + [""]
        lines += ["- **Drops:**", ""]
        lines += ["  - %s" % d for d in h["drops"]] + [""]
        lines += ["- **Durable anchor:** %s" % h["anchor"], ""]
        if h["markers"]:
            lines += ["- **Transcript markers audited:** %s"
                      % "; ".join(h["markers"]), ""]
        lines += ["- **Transcript audit:** %s" % h["audit"],
                  "- **Confidence:** %s" % h["confidence"],
                  "- **Evidence:** %s" % h["evidence"], ""]
    if compare_pair:
        a_id, b_id = compare_pair
        c = compare(a_id, b_id)
        lines += ["## Pairwise: %s vs %s" % (get(a_id)["name"],
                                             get(b_id)["name"]), ""]
        lines += ["### Drops unique to %s" % get(a_id)["name"], ""]
        lines += (["- %s" % d for d in c["drops_only_in_a"]] or
                  ["- (none -- same drop profile)"]) + [""]
        lines += ["### Drops unique to %s" % get(b_id)["name"], ""]
        lines += (["- %s" % d for d in c["drops_only_in_b"]] or
                  ["- (none -- same drop profile)"]) + [""]
        lines += ["### Survives unique to %s" % get(a_id)["name"], ""]
        lines += (["- %s" % s for s in c["survives_only_in_a"]] or
                  ["- (none)"]) + [""]
        lines += ["### Survives unique to %s" % get(b_id)["name"], ""]
        lines += (["- %s" % s for s in c["survives_only_in_b"]] or
                  ["- (none)"]) + [""]
    lines += ["---",
              "_Heuristic registry: entries marked 'community' are widely "
              "reported but not formally verified; 'measured' means someone "
              "ran repeated compactions and counted. Verify before "
              "trusting._", ""]
    return "\n".join(lines)
