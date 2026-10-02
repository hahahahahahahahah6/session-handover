"""Compaction diff audit for coding-agent session transcripts.

Failure mode this addresses: ``/compact`` in Claude Code silently drops
things the session had established -- project rules, decisions, open TODOs
(see claude-code#67500, claudefa.st's "What Survives /compact" table, and
mikepurvis's HN thread asking for a formal framework of what survives
compaction). Nothing audits that diff, so this does it mechanically.

Pipeline:
  1. Walk the transcript, find compaction boundaries: real /compact
     markers (system compact_boundary, isCompactSummary messages) and the
     continuation preamble. Bare {"type": "summary"} lines are session
     titles, not compactions, unless markers are present.
  2. From the turns BEFORE each boundary, heuristically extract candidate
     durable items: rules/constraints, TODOs, decisions, user preferences.
  3. Check each item against the summary text that REPLACES those turns.
     Matching is token-overlap (documented threshold), not semantic.
     Items with no match are reported as DROPPED.
  4. Report to terminal / Markdown. Never blocks (exit 0) unless
     --fail-on-drop is passed.

No LLM involved. stdlib only. All matching is deliberately conservative:
this finds *obviously stated* rules, not subtly implied ones.
"""

import re

from . import parsers

# A compaction boundary is where earlier turns were replaced by a summary.
# Real /compact evidence (undocumented, from observed transcripts):
#   - a system entry with subtype "compact_boundary", and/or
#   - a user entry flagged isCompactSummary carrying the replacement text.
# Bare {"type": "summary"} lines are session TITLES (the /resume list), not
# compactions -- they only count as boundaries when the transcript also
# carries real compaction markers (see _has_compact_markers). A resumed
# session also starts with a well-known continuation preamble, which is
# kept as a boundary (verified correct).
_SUMMARY_TYPE = "summary"
_COMPACT_BOUNDARY_SUBTYPE = "compact_boundary"
_CONTINUATION_PREFIXES = (
    "this session is being continued from a previous conversation",
    "this conversation is being continued from a previous session",
    "continuing from a previous conversation",
    "previous conversation that ran out of context",
)
# Fallback markers for Codex-shaped or otherwise unknown transcripts.
_COMPACT_MARKERS = ("compacted", "compacting", "/compact")


def _has_compact_markers(path):
    """True if the transcript contains real /compact evidence.

    A real /compact writes a system entry with subtype "compact_boundary"
    and injects a user message flagged isCompactSummary. Without either,
    bare {"type": "summary"} lines are just session titles.
    """
    for obj in parsers._lines(path):
        if obj.get("subtype") == _COMPACT_BOUNDARY_SUBTYPE:
            return True
        if obj.get("isCompactSummary"):
            return True
    return False


def _claude_events(path, summaries_are_boundaries=True):
    """Yield (kind, text) events in transcript order for a Claude transcript.

    kind is one of "text" (user/assistant text, pre- or post-compact) or
    "summary" (a compaction boundary whose text is the replacement summary).

    summaries_are_boundaries: only True when the transcript carries real
    compaction markers. Bare type=summary lines are session titles; they
    are skipped unless markers exist, and a summary before any text turn
    replaces nothing so it can never be a boundary. The isCompactSummary
    user message and the continuation preamble are always boundaries.
    """
    seen_text = False
    for obj in parsers._lines(path):
        otype = obj.get("type", "")
        if otype == _SUMMARY_TYPE:
            if summaries_are_boundaries and seen_text:
                # Known shape: {"type": "summary", "summary": "...", ...}.
                # Be tolerant: look for the text under a few plausible keys.
                text = (obj.get("summary") or obj.get("text")
                        or obj.get("content") or "")
                if isinstance(text, str) and text.strip():
                    yield ("summary", text.strip())
            continue
        if otype not in ("user", "assistant"):
            continue
        msg = obj.get("message")
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", otype)
        if role not in ("user", "assistant"):
            continue
        text = parsers._first_text(msg.get("content", []))
        if not parsers._is_genuine_user_text(text):
            continue
        if obj.get("isCompactSummary"):
            # The injected post-/compact summary: a real boundary.
            # Checked before the continuation preamble so the same message
            # is never counted twice (real injected summaries start with
            # the preamble text).
            yield ("summary", text.strip())
            continue
        lowered = text.strip().lower()
        if any(lowered.startswith(p) for p in _CONTINUATION_PREFIXES):
            # Continuation preamble: the rest of this message is the
            # compacted summary of everything before it.
            yield ("summary", text.strip())
            continue
        seen_text = True
        yield ("text", text.strip())


def _codex_events(path):
    """Yield (kind, text) events for a Codex CLI transcript.

    Codex has no documented compaction shape; this is best-effort: a
    summary-typed payload, or a message whose text looks like a compaction
    marker, counts as a boundary. If nothing matches, no boundary is found
    and the caller fails soft.
    """
    for obj in parsers._lines(path):
        if obj.get("type") != "response_item":
            continue
        p = obj.get("payload")
        if not isinstance(p, dict):
            continue
        ptype = str(p.get("type", ""))
        if "summary" in ptype:
            for key in ("text", "summary", "content"):
                text = p.get(key)
                if isinstance(text, str) and text.strip():
                    yield ("summary", text.strip())
                    break
            continue
        if ptype == "message":
            text = parsers._first_text(p.get("content", []))
            if not text.strip():
                continue
            lowered = text.strip().lower()
            if any(m in lowered for m in _COMPACT_MARKERS):
                yield ("summary", text.strip())
            else:
                yield ("text", text.strip())


def find_boundaries(path, tool):
    """Return (pre_turns, boundaries) for a transcript.

    pre_turns: list of (turn_index, text) for user/assistant text messages
    in transcript order. boundaries: list of (turn_index, summary_text)
    where turn_index is the index of the text turn the summary replaces
    (i.e. the summary appears right after it). turn_index is 1-based and
    counts only text-bearing turns plus summaries, so it can be used to
    cite the source in the original transcript.

    Bare {"type": "summary"} lines (session titles) are only treated as
    boundaries when the transcript carries real compaction markers
    (system compact_boundary / isCompactSummary); see _has_compact_markers.
    """
    if tool == "claude-code":
        events = _claude_events(
            path, summaries_are_boundaries=_has_compact_markers(path))
    else:
        events = _codex_events(path)
    turns = []
    boundaries = []
    for kind, text in events:
        if kind == "summary":
            # The summary replaces all text turns seen so far.
            boundaries.append((len(turns), text))
        else:
            turns.append((len(turns) + 1, text))
    return turns, boundaries


# ---------------------------------------------------------------------------
# Durable-item extraction (heuristic, conservative, no LLM)
# ---------------------------------------------------------------------------

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\-])")

_RULE_PATTERNS = [
    (r"\bnever\s+\w+", "rule"),
    (r"\balways\s+\w+", "rule"),
    (r"\bdo\s+not\s+\w+", "rule"),
    (r"\bdon'?t\s+\w+", "rule"),
    (r"\bmust\s+not\b", "rule"),
    (r"\bmake\s+sure\b", "rule"),
    (r"\bremember\s+to\b", "rule"),
    (r"\bdon'?t\s+forget\b", "rule"),
    (r"\bno\s+(?:commits?|push(?:es)?|force(?:-|\s)?push)\b", "rule"),
    (r"\bask\s+(?:me\s+)?before\b", "rule"),
]
_TODO_PATTERNS = [
    (r"^\s*(?:[-*]|\d+[.)])\s*\[ \]", "todo"),
    (r"\bTODO\b", "todo"),
    (r"\bstill\s+need\s+to\b", "todo"),
    (r"\bnot\s+(?:done|finished|complete[dy]?)\b", "todo"),
    (r"\bleft\s+to\s+(?:do|fix|implement|write)\b", "todo"),
    (r"\bremaining\b", "todo"),
]
_DECISION_PATTERNS = [
    (r"\bdecided\s+(?:to|on)\b", "decision"),
    (r"\bwe'?ll\s+(?:use|go\s+with|stick\s+with)\b", "decision"),
    (r"\bgoing\s+with\b", "decision"),
    (r"\blet'?s\s+(?:use|go\s+with|stick\s+with)\b", "decision"),
    (r"\bagreed\s+(?:to|on|that)\b", "decision"),
    (r"\bchose\b", "decision"),
    (r"\bswitched\s+to\b", "decision"),
    (r"\bbecause\b.{0,60}\b(?:use|chosen|picked|went)\b", "decision"),
]
_PREFERENCE_PATTERNS = [
    (r"\bi\s+prefer\b", "preference"),
    (r"\bi\s+like\b", "preference"),
    (r"\bmy\s+(?:name|email|project|repo)\s+is\b", "preference"),
    (r"\bi\s+am\b", "preference"),
    (r"\bwe\s+use\b", "preference"),
]

_ALL_PATTERNS = [(re.compile(p, re.IGNORECASE), kind)
                 for p, kind in (_RULE_PATTERNS + _TODO_PATTERNS
                                 + _DECISION_PATTERNS + _PREFERENCE_PATTERNS)]


def extract_items(turns):
    """Extract candidate durable items from pre-compact turns.

    turns: list of (turn_index, text). Returns a list of dicts with keys
    kind, text, turn. Only sentences matching a pattern are kept, so this
    deliberately misses subtly-phrased rules -- it is a recall-floor, not a
    semantic reader. Source turn index is kept for citation.
    """
    items = []
    seen = set()
    for turn_index, text in turns:
        for sentence in _SENTENCE_SPLIT.split(text):
            sentence = " ".join(sentence.split())
            if len(sentence) < 12 or len(sentence) > 600:
                continue
            for pattern, kind in _ALL_PATTERNS:
                if pattern.search(sentence):
                    key = (kind, sentence.lower())
                    if key not in seen:
                        seen.add(key)
                        items.append({"kind": kind, "text": sentence,
                                      "turn": turn_index})
                    break
    return items


# ---------------------------------------------------------------------------
# Survival check: token overlap, not semantic similarity
# ---------------------------------------------------------------------------

_STOPWORDS = frozenset("""
a an the and or but of to in on for with by at from as is are was were be
been being it its this that these those i you he she we they them him her
us my your his our their me do does did not no not so if then than too very
just only also will would should could can cannot have has had having are
the there their what when where which who whom how why all any both each
few more most other some such into over after before between through during
up down out off again once here there when where why how all any both each
few more most other some such no nor only own same than too very can will
just don should now
""".split())

# A summary keeps an item if at least this fraction of the item's
# distinctive tokens appears in the summary. Documented here and in the
# README; not semantic -- a reworded rule that shares no vocabulary will
# be reported as dropped even if a human would say it survived.
OVERLAP_THRESHOLD = 0.5


def _tokens(text):
    return {t for t in re.findall(r"[a-z0-9]+", text.lower())
            if t not in _STOPWORDS and len(t) > 2}


def overlap_ratio(item_text, summary_text):
    """Fraction of the item's distinctive tokens present in the summary."""
    item_tokens = _tokens(item_text)
    if not item_tokens:
        return 0.0
    summary_tokens = _tokens(summary_text)
    return len(item_tokens & summary_tokens) / len(item_tokens)


def check_survival(items, summary_text):
    """Split items into (survived, dropped) against one summary."""
    survived, dropped = [], []
    for item in items:
        ratio = overlap_ratio(item["text"], summary_text)
        if ratio >= OVERLAP_THRESHOLD:
            survived.append(item)
        else:
            dropped.append(item)
    return survived, dropped


# ---------------------------------------------------------------------------
# Audit
# ---------------------------------------------------------------------------

def audit(path, tool):
    """Audit one transcript. Returns a dict with per-boundary results.

    Never raises on bad input: unparseable files yield zero boundaries and
    are reported as "no compaction boundary found" downstream.
    """
    try:
        turns, boundaries = find_boundaries(path, tool)
    except Exception:
        turns, boundaries = [], []
    results = []
    for boundary_index, summary_text in boundaries:
        pre = turns[:boundary_index]
        items = extract_items(pre)
        survived, dropped = check_survival(items, summary_text)
        results.append({
            "summary": summary_text,
            "extracted": items,
            "survived": survived,
            "dropped": dropped,
        })
    return {"path": path, "tool": tool, "boundaries": results}


def _truncate(text, limit=300):
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit] + "..."


def render_markdown(audit_result, session_id=""):
    """Render the full audit as Markdown."""
    r = audit_result
    lines = ["# Compaction Audit", ""]
    lines.append("- **Tool:** %s" % r.get("tool", "?"))
    if session_id:
        lines.append("- **Session:** `%s`" % session_id)
    lines.append("- **Transcript:** `%s`" % r.get("path", "?"))
    lines.append("")
    if not r["boundaries"]:
        lines.append("No compaction boundary found in this transcript.")
        lines.append("")
        return "\n".join(lines)
    total_extracted = sum(len(b["extracted"]) for b in r["boundaries"])
    total_dropped = sum(len(b["dropped"]) for b in r["boundaries"])
    lines.append("**Overall:** %d durable items extracted, %d survived, "
                 "%d dropped." % (total_extracted,
                                  total_extracted - total_dropped,
                                  total_dropped))
    lines.append("")
    for i, b in enumerate(r["boundaries"], 1):
        lines.append("## Boundary %d" % i)
        lines.append("")
        lines.append("Extracted %d items: %d survived, %d dropped."
                     % (len(b["extracted"]), len(b["survived"]),
                        len(b["dropped"])))
        lines.append("")
        if b["dropped"]:
            lines.append("### Dropped")
            lines.append("")
            for item in b["dropped"]:
                lines.append("- **[%s]** (source: turn %d)" %
                             (item["kind"], item["turn"]))
                lines.append("  - Item: %s" % item["text"])
                lines.append("  - Suggested restore: `%s`" % item["text"])
            lines.append("")
        if b["survived"]:
            lines.append("### Survived")
            lines.append("")
            for item in b["survived"]:
                lines.append("- **[%s]** (source: turn %d): %s" %
                             (item["kind"], item["turn"],
                              _truncate(item["text"], 160)))
            lines.append("")
        lines.append("### Summary text (what replaced the turns)")
        lines.append("")
        lines.append("> " + _truncate(b["summary"], 800).replace("\n", "\n> "))
        lines.append("")
    lines.append("---")
    lines.append("_Heuristic audit: extraction misses subtly-phrased rules, "
                 "and matching is token-overlap (threshold %.1f), not semantic. "
                 "Verify before trusting._" % OVERLAP_THRESHOLD)
    lines.append("")
    return "\n".join(lines)


def render_terminal(audit_result, session_id=""):
    """Compact terminal version of the report."""
    r = audit_result
    out = []
    if not r["boundaries"]:
        return "No compaction boundary found in this transcript."
    for i, b in enumerate(r["boundaries"], 1):
        out.append("boundary %d: extracted=%d survived=%d dropped=%d" %
                   (i, len(b["extracted"]), len(b["survived"]),
                    len(b["dropped"])))
        for item in b["dropped"]:
            out.append("  DROPPED [%s] (turn %d): %s" %
                       (item["kind"], item["turn"],
                        _truncate(item["text"], 120)))
            out.append("    restore: %s" % _truncate(item["text"], 140))
    return "\n".join(out)
