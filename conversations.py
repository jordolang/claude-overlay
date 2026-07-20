# -*- coding: utf-8 -*-
"""List and load prior Claude conversations so the overlay can RESUME one instead of
always starting blank.

The `claude` CLI stores one JSONL transcript per session under
``~/.claude/projects/<escaped-cwd>/<session-id>.jsonl``. This module reads those files
to (a) list a project's past sessions for a picker and (b) flatten a chosen session's
real conversational turns for on-screen replay. The session id (the filename stem) is
what the SDK's ``resume`` option needs.

Pure logic — no Tk, no SDK, no network — so it's safe to call off the UI thread and is
unit-testable."""

import json
import os

PROJECTS_ROOT = os.path.expanduser("~/.claude/projects")

# User "messages" the CLI writes that aren't real user prose: slash-command envelopes,
# bash IO echoes, injected memory/caveat blocks. Dropped from titles and replay.
_META_PREFIXES = (
    "<local-command", "<command-name", "<command-message", "<command-args",
    "<bash-input", "<bash-stdout", "<bash-stderr", "<user-memory", "caveat:",
)


def project_dir(working_dir):
    """The ~/.claude/projects sub-dir holding this cwd's session transcripts. The CLI
    escapes the absolute cwd by replacing every '/' with '-' (e.g. /Users/x → -Users-x)."""
    esc = str(working_dir).replace("/", "-")
    return os.path.join(PROJECTS_ROOT, esc)


def _text_of(content):
    """Flatten a message 'content' (a str, or a list of content blocks) to plain text.
    Returns '' for tool-only / image-only / non-text content."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [str(b.get("text", "")) for b in content
                 if isinstance(b, dict) and b.get("type") == "text"]
        return "\n".join(parts).strip()
    return ""


def _is_meta_user(text, rec):
    """True when a user record is bookkeeping (meta flag / command envelope / bash echo /
    empty), not something the person actually typed."""
    if rec.get("isMeta"):
        return True
    low = text.lstrip().lower()
    return (not text) or any(low.startswith(p) for p in _META_PREFIXES)


def _iter_turns(path):
    """Yield ('user'|'assistant', text) for each real conversational turn in a transcript,
    in order. Meta/command/tool-result/tool-only and non-text records are skipped."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get("type") not in ("user", "assistant"):
                    continue
                msg = rec.get("message")
                if not isinstance(msg, dict):
                    continue
                role = msg.get("role")
                text = _text_of(msg.get("content"))
                if role == "user":
                    if _is_meta_user(text, rec):
                        continue
                    yield "user", text
                elif role == "assistant" and text:
                    yield "assistant", text
    except Exception:
        return


def load_transcript(path, max_turns=400):
    """Return [{'role': 'user'|'assistant', 'text': str}, ...] — real turns only, for
    on-screen replay of a resumed conversation. Capped at max_turns (newest kept)."""
    turns = [{"role": r, "text": t} for r, t in _iter_turns(path)]
    return turns[-max_turns:]


def _summary(path, title_limit=72):
    """(title, n_turns) for the picker: title = first real user line, n_turns = count of
    real user+assistant turns. Single pass; ('', 0) for an empty/meta-only session."""
    title, n = None, 0
    for role, text in _iter_turns(path):
        n += 1
        if title is None and role == "user":
            s = " ".join(text.split())
            title = (s[: title_limit - 1] + "…") if len(s) > title_limit else s
    return (title or "", n)


def list_conversations(working_dir, limit=60):
    """List a project's past sessions, newest first, as
    [{'id', 'path', 'mtime', 'title', 'turns'}, ...]. Empty/meta-only sessions are omitted.
    'id' is the session id to hand to ClaudeWorker.resume()."""
    d = project_dir(working_dir)
    if not os.path.isdir(d):
        return []
    out = []
    for name in os.listdir(d):
        if not name.endswith(".jsonl"):
            continue
        path = os.path.join(d, name)
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            continue
        title, n = _summary(path)
        if n == 0:
            continue
        out.append({"id": name[:-6], "path": path, "mtime": mtime,
                    "title": title or "(untitled)", "turns": n})
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out[:limit]


def rel_time(mtime, now):
    """Compact human 'time ago' for a timestamp (both epoch seconds). `now` is passed in so
    this stays pure/testable (no clock read)."""
    d = max(0, int(now - mtime))
    if d < 60:
        return "just now"
    if d < 3600:
        return f"{d // 60}m ago"
    if d < 86400:
        return f"{d // 3600}h ago"
    if d < 7 * 86400:
        return f"{d // 86400}d ago"
    return f"{d // (7 * 86400)}w ago"
