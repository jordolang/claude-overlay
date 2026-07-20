#!/bin/bash
# Launch Claude Overlay on macOS using the project's virtualenv (Python 3.13 + Tk).
# macOS has no `pythonw`; a normal python launch shows the GUI with no console-window
# issue. Run from anywhere: ./run-macos.sh
cd "$(dirname "$0")" || exit 1
exec .venv/bin/python claude_overlay.py "$@"
