"""Terminal styles for human-readable progress. Match scripts/terminal.sh."""

import os
import sys

STYLES = {
    "heading": "1;36",
    "step": "1",
    "removed": "32",
    "would_remove": "36",
    "kept": "2",
    "skipped": "33",
    "failed": "1;31",
}


def styled(text, role):
    if sys.stdout.isatty() and not os.environ.get("NO_COLOR") and os.environ.get("TERM") != "dumb":
        return f"\033[{STYLES[role]}m{text}\033[0m"
    return text
