"""Logging setup shared by the server and the clients."""

from __future__ import annotations

import logging
import sys
from typing import IO, Optional

LOGGER_NAME = "desktalk"
_HANDLER_MARK = "_desktalk_handler"

SIMPLE_FORMAT = "[%(asctime)s] %(message)s"
VERBOSE_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
TIME_FORMAT = "%H:%M:%S"


def make_stream_safe(stream: Optional[IO[str]]) -> None:
    """Never let a character the console cannot encode (emoji, Devanagari, ...) raise.

    On Windows the console codepage is often cp437/cp1252; printing a chat
    message with other characters would otherwise throw UnicodeEncodeError.
    """
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is None:
        return
    try:
        reconfigure(errors="replace")
    except (OSError, ValueError):
        pass


def setup_logging(
    verbose: bool = False,
    log_file: Optional[str] = None,
    stream: Optional[IO[str]] = None,
) -> None:
    """Configure the ``desktalk`` logger. Safe to call more than once."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    logger.propagate = False

    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARK, False):
            logger.removeHandler(handler)
            handler.close()

    formatter = logging.Formatter(VERBOSE_FORMAT if verbose else SIMPLE_FORMAT, TIME_FORMAT)
    handlers: list[logging.Handler] = []

    stream = stream if stream is not None else sys.stderr
    if stream is not None:  # pythonw.exe has no stderr
        make_stream_safe(stream)
        handlers.append(logging.StreamHandler(stream))

    if log_file:
        try:
            handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
        except OSError as exc:
            if stream is not None:
                print("WARNING: Could not open log file {!r}: {}".format(log_file, exc), file=stream)

    for handler in handlers:
        handler.setFormatter(formatter)
        setattr(handler, _HANDLER_MARK, True)
        logger.addHandler(handler)
