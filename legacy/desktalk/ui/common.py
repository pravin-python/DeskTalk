"""Logic shared by the GUI and the CLI: input commands and defaults."""

from __future__ import annotations

import os
from dataclasses import dataclass

DM_USAGE = "use: /dm <naam> <message>"


@dataclass(frozen=True)
class Command:
    """What the user typed, classified.

    kind is one of: ``say``, ``dm``, ``who``, ``quit``, ``error``.
    """

    kind: str
    text: str = ""
    to: str = ""
    error: str = ""


def parse_input(line: str) -> "Command | None":
    """Classify one input line. None for an empty line."""
    line = line.strip()
    if not line:
        return None
    if line in ("/quit", "/exit"):
        return Command("quit")
    if line in ("/who", "/users"):
        return Command("who")
    if line == "/dm" or line.startswith("/dm "):
        parts = line[3:].strip().split(None, 1)
        if len(parts) < 2:
            return Command("error", error=DM_USAGE)
        return Command("dm", to=parts[0], text=parts[1])
    return Command("say", text=line)


def default_username() -> str:
    return os.environ.get("USERNAME") or os.environ.get("USER") or "user"
