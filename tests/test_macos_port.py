"""Guard tests for THIS FORK's macOS port — the things a merge from upstream can break.

Upstream (shengyanlin/claude-overlay) is Windows-only and knows nothing about this
fork's port or its Load-conversation feature, so none of it is covered by upstream's
suite. Every upstream release lands as a hand-resolved merge into `claude_overlay.py` /
`worker.py`, which is exactly where a resolution mistake would go unnoticed.

This file is new (upstream has no file by this name), so it never conflicts and keeps
working across releases. `update-macos.sh` runs it as the post-merge gate.

The centerpiece is test_no_duplicate_dispatch_branches. When v1.14.0 was merged, git's
auto-merge kept BOTH the fork's resume implementation and upstream's — the duplicated
`elif kind == "resume"` / `elif kind == "resumed"` branches landed OUTSIDE the conflict
markers, so the merge looked clean and compiled fine. Only the FIRST branch of an
if/elif chain ever runs, so the fork's version silently shadowed upstream's and resume
was broken with no error anywhere. Conflict markers are easy to spot; that is not.
"""
import ast
import os
import sys
from pathlib import Path

import pytest
from conftest import chat_text

import claude_overlay as co

REPO = Path(__file__).resolve().parent.parent


def _eq_literal(test):
    """('var', 'literal') for a `var == "literal"` test, else None."""
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    if not isinstance(test.ops[0], ast.Eq) or not isinstance(test.left, ast.Name):
        return None
    rhs = test.comparators[0]
    if isinstance(rhs, ast.Constant) and isinstance(rhs.value, str):
        return (test.left.id, rhs.value)
    return None


def _elif_chains(path):
    """Yield each if/elif chain in the file as (first_line, [(var, literal), ...]).

    Scoped to ONE chain rather than a whole function on purpose: two separate `if`
    statements may legitimately test the same value twice (a re-check after doing
    something), but within a single if/elif chain a repeat is unreachable by
    construction. Chains are found by their head — an `If` that is not itself the
    `orelse` of another `If`.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    nested = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.If) and len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            nested.add(id(node.orelse[0]))
    for node in ast.walk(tree):
        if not isinstance(node, ast.If) or id(node) in nested:
            continue
        chain, cur = [], node
        while True:
            hit = _eq_literal(cur.test)
            if hit:
                chain.append(hit)
            if len(cur.orelse) == 1 and isinstance(cur.orelse[0], ast.If):
                cur = cur.orelse[0]
            else:
                break
        if len(chain) > 1:
            yield node.lineno, chain


@pytest.mark.parametrize("module", ["worker.py", "claude_overlay.py"])
def test_no_duplicate_dispatch_branches(module):
    """No if/elif chain compares the same variable to the same string twice.

    A duplicate means the second branch is DEAD CODE that can never run — the signature
    of a merge that kept both sides of a rewritten handler. Fix by deleting whichever
    copy lost, not by renaming.
    """
    dupes = []
    for lineno, chain in _elif_chains(REPO / module):
        seen = set()
        for var, lit in chain:
            if (var, lit) in seen:
                dupes.append(f"{module}:{lineno} — `{var} == {lit!r}` appears twice in one "
                             f"if/elif chain (the second branch is unreachable)")
            seen.add((var, lit))
    assert not dupes, (
        "Duplicate dispatch branch(es) — almost certainly a bad merge that kept both "
        "the fork's and upstream's version of a handler:\n  " + "\n  ".join(dupes))


# ── the fork's resume path must keep working after an upstream merge ─────────────

def test_worker_has_exactly_one_resume_entry_point():
    """Upstream owns resume() now; the fork's duplicate one-liner must stay deleted."""
    tree = ast.parse((REPO / "worker.py").read_text(encoding="utf-8"))
    names = [n.name for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "resume"]
    assert len(names) == 1, f"expected 1 `def resume`, found {len(names)}"


def test_fork_resume_state_is_gone():
    """The fork's own `_resume_id` lost to upstream's `_resume_session_id` plumbing.

    If it reappears, a merge resurrected the fork's shadowed implementation.
    """
    src = (REPO / "worker.py").read_text(encoding="utf-8")
    assert "_resume_id" not in src.replace("_resume_id_", ""), \
        "worker._resume_id is back — upstream's _resume_session_id path is being shadowed"
    assert "_resume_session_id" in src, "upstream's resume plumbing went missing"


def test_conversation_picker_still_wired():
    """The fork's Load-conversation feature (upstream has none) survived the merge."""
    assert (REPO / "conversations.py").exists()
    for attr in ("_load_conversation", "_open_conv_picker", "_render_history_assistant"):
        assert hasattr(co.Overlay, attr), f"Overlay.{attr} went missing"


def test_load_conversation_replays_and_resumes(overlay, tmp_path, monkeypatch):
    """_load_conversation replays the transcript, flags the replay, and asks to resume."""
    fake = tmp_path / "sess.jsonl"
    fake.write_text("")
    import conversations
    monkeypatch.setattr(conversations, "load_transcript",
                        lambda p: [{"role": "user", "text": "hello there"},
                                   {"role": "assistant", "text": "hi back"}])
    # User turns render into an embedded bubble WIDGET, not into the Text content, so
    # chat_text() can't see them — record the calls instead.
    users = []
    monkeypatch.setattr(overlay, "add_user", lambda t: users.append(t))

    overlay._load_conversation({"id": "sess-abc", "path": str(fake), "title": "My chat"})

    text = chat_text(overlay)
    assert users == ["hello there"], "the user turn was not replayed"
    assert "hi back" in text, "the assistant turn was not replayed"
    assert "My chat" in text
    assert overlay._replayed_resume is True
    assert ("resume", ("sess-abc",)) in overlay.worker.calls, "worker.resume() not called"


def test_resumed_after_picker_does_not_claim_no_replay(overlay):
    """The picker already painted the turns, so the 'not replayed here' line is wrong."""
    overlay._replayed_resume = True
    overlay._handle("resumed", "sess-abc")
    assert "isn't replayed here" not in chat_text(overlay)
    assert overlay._replayed_resume is False, "the flag must be consumed, not left set"


def test_resumed_from_launch_button_still_announces(overlay):
    """Without a preceding replay (upstream's launch-time Resume button) it must announce."""
    overlay._replayed_resume = False
    overlay._handle("resumed", "sess-abc")
    assert "isn't replayed here" in chat_text(overlay)


def test_resume_failed_clears_the_replay_flag(overlay):
    """A stale True would silence the announcement on the NEXT, unrelated resume."""
    overlay._replayed_resume = True
    overlay._handle("resume_failed", None)
    assert overlay._replayed_resume is False


# ── the macOS port itself ────────────────────────────────────────────────────────

def test_win32utils_imports_and_degrades_off_windows():
    """win32utils must import on macOS (the _NoWin shim) rather than blow up on windll."""
    import win32utils
    assert hasattr(win32utils, "IS_WIN")
    if not win32utils.IS_WIN:
        # The shim's attributes must be callable no-ops, not AttributeErrors.
        assert win32utils._user32.AnyUndefinedWin32Call() == 0


def test_resize_cursors_are_aqua_valid():
    """Tk on macOS rejects the Win32 cursor names; _cursor() maps them to native ones.

    A bad value doesn't raise at import — it throws TclError when the widget is built,
    i.e. at launch, so this is worth pinning.
    """
    import sys
    assert callable(getattr(co, "_cursor", None)), "the _cursor() shim went missing"
    if sys.platform == "darwin":
        for win_name in ("size_ns", "size_we", "size_nw_se", "size_ne_sw"):
            assert co._cursor(win_name) != win_name, \
                f"{win_name} is not valid on Aqua — it must be remapped"


def test_hotkey_registration_is_skipped_off_windows():
    """The `keyboard` global hotkey needs root on macOS and throws in a bg thread."""
    src = (REPO / "claude_overlay.py").read_text(encoding="utf-8")
    i = src.index("def _register_hotkey")
    body = src[i:i + 1200]
    assert ("IS_WIN" in body or "platform" in body or "darwin" in body), \
        "_register_hotkey lost its non-Windows guard — it will throw on macOS"


def test_mac_keyable_repair_survives():
    """Without _mac_ensure_keyable the text box silently stops accepting keystrokes.

    (canBecomeKeyWindow goes NO once deiconify/overrideredirect strips the Titled bit —
    see the macos-port-setup notes. The repair is Tcl-side MacWindowStyle, NOT pyobjc
    setStyleMask, which hangs this app's run loop.)
    """
    src = (REPO / "claude_overlay.py").read_text(encoding="utf-8")
    for marker in ("_mac_ensure_keyable", "MacWindowStyle", "makeKeyAndOrderFront_",
                   "setActivationPolicy_", "_frameless_reassert"):
        assert marker in src, f"macOS typing fix lost `{marker}` — the text box will go dead"

    # setStyleMask must never actually be CALLED. The name legitimately appears in a
    # comment warning against it, so check the AST for a real attribute access rather
    # than grepping the text.
    called = [n for n in ast.walk(ast.parse(src))
              if isinstance(n, ast.Attribute) and n.attr == "setStyleMask"]
    assert not called, ("pyobjc setStyleMask is called again — it HANGS this app's run "
                        "loop. Use the Tcl-side MacWindowStyle instead.")


def test_launcher_scripts_present():
    """The macOS launcher/updater the LaunchAgent points at."""
    for name in ("run-macos.sh", "update-macos.sh"):
        p = REPO / name
        assert p.exists(), f"{name} is missing"
        assert os.access(p, os.X_OK), f"{name} is not executable (chmod +x)"


def test_user_facing_hints_never_send_mac_users_to_cmd_files():
    """Upstream's error text names .cmd/.ps1 — dead ends on macOS.

    `./update.cmd` is batch, so the shell just says "command not found"; the advice reads
    as a fix and isn't one. The `git pull` fallback is worse than useless here: this clone
    carries the port as commits on top of upstream, so a pull fetches nothing and reports
    success (that's the whole reason update-macos.sh merges origin/main instead).

    An upstream merge re-hardcoding those strings would silently undo the fix, so pin the
    platform switch rather than the wording.
    """
    # worker.py resolves its hints at import, so assert the values the user actually sees
    # rather than the source that produced them — reflowing the code can't defeat this.
    if sys.platform == "darwin":
        import worker
        assert "update-macos.sh" in worker.UPDATE_HINT, \
            "worker's update hint lost the macOS updater"
        assert ".cmd" not in worker.UPDATE_HINT, \
            "worker tells macOS users to run a Windows batch file"
        assert ".ps1" not in worker.INSTALL_CLI_HINT and ".cmd" not in worker.INSTALL_CLI_HINT, \
            "worker's CLI-install hint points macOS users at a PowerShell/batch script"

    # The overlay builds its hint inline as `X if MAC else Y`, so it has to be read out of
    # the source. Text-matching the line is no good — the Windows half names `git pull` on
    # that same line, and the comment above it names both — so pull the mac branch out of
    # the AST and assert on that alone.
    src = (REPO / "claude_overlay.py").read_text(encoding="utf-8")
    mac_branches = [
        n.body.value for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.IfExp) and isinstance(n.body, ast.Constant)
        and isinstance(n.body.value, str) and "update-macos.sh" in n.body.value
    ]
    assert mac_branches, "the update notice lost its `... if MAC else ...` macOS updater"
    for hint in mac_branches:
        assert "git pull" not in hint, \
            "the macOS update hint offers `git pull`, which silently no-ops on this fork"
        assert ".cmd" not in hint, \
            "the macOS update hint names a Windows batch file"
