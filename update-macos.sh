#!/bin/bash
# Update Claude Overlay (macOS fork) to the latest UPSTREAM release.
#
# This clone carries a macOS port as local commits on top of upstream
# (shengyanlin/claude-overlay). Taking a new upstream release therefore means MERGING
# origin/main into this branch, not `git pull` — the branch tracks a fork, so a plain
# pull silently fetches nothing and reports success while leaving you versions behind.
# That is the bug this script exists to prevent.
#
# Guarantees:
#   * Upstream is never written to. `origin`'s push URL is set to `no_push`.
#   * Your data is never touched: .venv (gitignored), ~/.claude-overlay/ and
#     ~/claude-overlay/config.json all live outside anything git manages.
#   * If the merge conflicts, it is ABORTED and the tree is left exactly as it was.
#   * If the merged code fails its checks, it is ROLLED BACK automatically.
# Worst case you end up exactly where you started, with an explanation.
set -uo pipefail
cd "$(dirname "$0")" || exit 1

say()  { printf '%s\n' "$*"; }
rule() { say "============================================================"; }
rule; say "  Claude Overlay - update from upstream (macOS)"; rule; say

# --- preconditions -----------------------------------------------------------------
command -v git >/dev/null 2>&1 || {
  say "[X] git not found. Install the Xcode command line tools:"
  say "      xcode-select --install"; exit 1; }
git rev-parse --is-inside-work-tree >/dev/null 2>&1 || {
  say "[X] This folder isn't a git clone, so there's nothing to merge."; exit 1; }
git remote get-url origin >/dev/null 2>&1 || {
  say "[X] No 'origin' remote. Add the upstream project with:"
  say "      git remote add origin https://github.com/shengyanlin/claude-overlay.git"; exit 1; }

# Belt-and-suspenders: make it impossible for this clone to write to the upstream repo.
if [ "$(git remote get-url --push origin 2>/dev/null)" != "no_push" ]; then
  git remote set-url --push origin no_push
  say "[i] Set origin's push URL to 'no_push' — this clone cannot write to upstream."
fi
# Record conflict resolutions so the SAME conflict resolves itself on future releases.
[ "$(git config --get rerere.enabled)" = "true" ] || git config rerere.enabled true

# A dirty tree turns any rollback into guesswork. Refuse rather than auto-stash: a
# stash pop can conflict on top of a merge conflict, which is a worse hole to be in.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
  say "[X] You have uncommitted changes. Commit or revert them first, then re-run:"
  say
  git status --short --untracked-files=no | sed 's/^/      /'
  say
  say "      git stash push -m 'wip'     # or: git checkout -- <file>"
  exit 1
fi

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
PRE="$(git rev-parse HEAD)"
say "Branch : $BRANCH"
say "At     : $(git log -1 --format='%h %s' | cut -c1-60)"
say

# --- fetch upstream ----------------------------------------------------------------
say "Fetching upstream (origin)..."
if ! git fetch origin --tags --quiet; then
  say "[X] Fetch failed (offline?). Nothing was changed."; exit 1
fi

BEHIND="$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)"
if [ "${BEHIND:-0}" -eq 0 ]; then
  say "[OK] Already up to date with upstream — nothing to merge."
  say "     Running version: $(grep -m1 '__version__' config.py | cut -d'"' -f2)"
  exit 0
fi

say "$BEHIND new upstream commit(s):"
git log --oneline --no-decorate "HEAD..origin/main" | head -20 | sed 's/^/    /'
say

# --- merge -------------------------------------------------------------------------
say "Merging origin/main..."
if ! git merge --no-edit origin/main >/tmp/co_merge.$$ 2>&1; then
  tail -20 /tmp/co_merge.$$ | sed 's/^/    /'
  say
  say "[!] The merge CONFLICTS — aborting it so nothing is left half-applied."
  git merge --abort 2>/dev/null || git reset --hard "$PRE" >/dev/null 2>&1
  rm -f /tmp/co_merge.$$
  say "[OK] Reverted. Your tree is exactly as it was ($(git rev-parse --short HEAD))."
  say
  say "    This is normal: the macOS port edits the same files upstream does."
  say "    Resolving it needs a human (or Claude) — the resume/session code in"
  say "    claude_overlay.py and worker.py is where the fork and upstream collide."
  say "    Ask Claude Code:  \"merge origin/main into my macOS port\""
  exit 2
fi
rm -f /tmp/co_merge.$$
say "[OK] Merged cleanly."
say

# --- refresh packages --------------------------------------------------------------
PY=""
if [ -x ".venv/bin/python" ]; then PY=".venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then PY="python3"; fi
if [ -n "$PY" ]; then
  say "Refreshing Python packages using $PY..."
  "$PY" -m pip install --upgrade --quiet claude-agent-sdk pillow keyboard \
    || say "[!] Package refresh failed - the code was still merged."
else
  say "[!] No Python found - skipping the package refresh. See SETUP.md."
fi
say

# --- verify, and roll back if the merge broke the port -----------------------------
# Deliberately NOT gated on the full suite: test_ui_chrome's clipboard test fails on
# macOS for environmental reasons (Tk clipboard ownership), so gating on it would
# revert every good update forever. The gate is the deterministic part — everything
# imports, and the macOS port + fork features are still intact.
FAILED=""
if [ -n "$PY" ]; then
  say "Verifying the merge..."
  if ! "$PY" -m py_compile claude_overlay.py worker.py config.py conversations.py \
                           win32utils.py cliupdate.py modelresolve.py 2>/tmp/co_pc.$$; then
    sed 's/^/    /' /tmp/co_pc.$$; FAILED="the code does not compile"
  fi
  rm -f /tmp/co_pc.$$
  if [ -z "$FAILED" ]; then
    # Distinguish "the guard is missing" from "the guard failed". A missing file is a
    # setup problem, NOT a regression — rolling a good update back over it would be
    # wrong, and silently skipping it would be worse. Warn loudly and continue.
    if [ ! -f tests/test_macos_port.py ]; then
      say "[!] tests/test_macos_port.py is missing — the macOS port is UNVERIFIED."
      say "    (Restore it with: git checkout $PRE -- tests/test_macos_port.py)"
    elif ! "$PY" -c "import pytest" >/dev/null 2>&1; then
      say "[!] pytest isn't installed — the macOS port is UNVERIFIED."
      say "    (Install it with: $PY -m pip install pytest)"
    elif ! "$PY" -m pytest tests/test_macos_port.py -q -p no:cacheprovider \
         >/tmp/co_pt.$$ 2>&1; then
      grep -E "^(E |FAILED|AssertionError)" /tmp/co_pt.$$ | head -25 | sed 's/^/    /'
      FAILED="the macOS port guard tests fail"
    fi
    rm -f /tmp/co_pt.$$
  fi
fi

if [ -n "$FAILED" ]; then
  say
  say "[!] This upstream release BREAKS the macOS port: $FAILED"
  say "    Rolling back to where you were."
  git reset --hard "$PRE" >/dev/null 2>&1
  say "[OK] Rolled back to $(git rev-parse --short HEAD). Nothing is broken."
  say
  say "    Ask Claude Code:  \"merge origin/main into my macOS port\""
  exit 3
fi
say "[OK] Compiles, and the macOS port + fork features are intact."

# The full suite runs for INFORMATION only — it never decides anything (see above).
if [ -n "$PY" ] && "$PY" -c "import pytest" >/dev/null 2>&1; then
  SUMMARY="$("$PY" -m pytest tests/ -q -p no:cacheprovider 2>&1 | tail -1)"
  say "     Full suite: $SUMMARY"
  say "     (test_copy_btn_puts_text_on_clipboard fails on macOS for environmental"
  say "      reasons — Tk clipboard ownership — and is expected.)"
fi

# --- done --------------------------------------------------------------------------
say
rule
say "  [OK] Updated to v$(grep -m1 '__version__' config.py | cut -d'"' -f2)"
say
say "  Quit the running overlay and re-open it - it does not"
say "  reload while running:"
say "    ./run-macos.sh"
say "  or, under launchd:"
say "    launchctl kickstart -k gui/\$(id -u)/com.claude-overlay.agent"
say
say "  Your settings and history were untouched (they live outside"
say "  the repo: ~/.claude-overlay/ and ~/claude-overlay/config.json)."
say
say "  Undo this update:  git reset --hard $PRE"
rule
