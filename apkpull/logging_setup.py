"""Verbose, human-friendly logging setup.

``-v`` / ``-vv`` on the CLI map to INFO / DEBUG. Output is colorized when
attached to a TTY. Every log call site includes the device label in the
message itself (``"[Pixel 7] ..."``) since multiple devices log concurrently
from a thread pool and a single shared stream is simplest to reason about.
"""

from __future__ import annotations

import logging
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from rich.console import Console

_LEVEL_COLORS = {
    logging.DEBUG: "\x1b[36m",  # cyan
    logging.INFO: "\x1b[32m",  # green
    logging.WARNING: "\x1b[33m",  # yellow
    logging.ERROR: "\x1b[31m",  # red
    logging.CRITICAL: "\x1b[41m",  # red bg
}
_RESET = "\x1b[0m"

_RICH_LEVEL_STYLES = {
    logging.DEBUG: "cyan",
    logging.INFO: "green",
    logging.WARNING: "yellow",
    logging.ERROR: "red",
    logging.CRITICAL: "white on red",
}


class _ColorFormatter(logging.Formatter):
    def __init__(self, *, use_color: bool, debug: bool) -> None:
        fmt = (
            "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
            if debug
            else "%(levelname)-8s %(message)s"
        )
        super().__init__(fmt, datefmt="%H:%M:%S")
        self.use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        message = super().format(record)
        if not self.use_color:
            return message
        color = _LEVEL_COLORS.get(record.levelno, "")
        return f"{color}{message}{_RESET}" if color else message


class _RichConsoleHandler(logging.Handler):
    """Prints through a rich ``Console`` instead of writing straight to a
    stream — required when a live display is active on that same console:
    plain direct writes would corrupt its region, but anything printed via
    the console itself is automatically coordinated to appear above it."""

    def __init__(self, console: Console, *, formatter: logging.Formatter) -> None:
        super().__init__()
        self.console = console
        self.setFormatter(formatter)

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = self.format(record)
            self.console.print(
                message, style=_RICH_LEVEL_STYLES.get(record.levelno), markup=False
            )
        except Exception:  # noqa: BLE001 - logging handlers must never raise
            self.handleError(record)


def setup_logging(
    verbosity: int, *, stream=None, console: Console | None = None
) -> None:
    """Configure the ``apkpull`` logger tree. Call once, from the CLI entry point.

    ``console`` routes output through a shared rich ``Console`` instead of
    writing directly to ``stream`` — pass the same console a
    :class:`apkpull.tui.LiveDisplay` is using so log records print correctly
    above its live region instead of corrupting it.
    """
    level = {0: logging.WARNING, 1: logging.INFO}.get(verbosity, logging.DEBUG)

    if console is not None:
        handler: logging.Handler = _RichConsoleHandler(
            console, formatter=_ColorFormatter(use_color=False, debug=verbosity >= 2)
        )
    else:
        stream = stream or sys.stderr
        handler = logging.StreamHandler(stream)
        handler.setFormatter(
            _ColorFormatter(use_color=stream.isatty(), debug=verbosity >= 2)
        )

    logger = logging.getLogger("apkpull")
    logger.setLevel(level)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False
