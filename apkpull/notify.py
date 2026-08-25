"""Best-effort native desktop notifications.

macOS uses ``osascript`` (always present); Linux uses ``notify-send`` when
available. Any failure here is swallowed — a missing notifier must never
fail a pull.
"""

from __future__ import annotations

import logging
import platform
import shutil
import subprocess

logger = logging.getLogger("apkpull.notify")


def notify(title: str, message: str) -> None:
    system = platform.system()
    try:
        if system == "Darwin":
            script = f"display notification {_osascript_quote(message)} with title {_osascript_quote(title)}"
            subprocess.run(
                ["osascript", "-e", script], capture_output=True, timeout=5, check=False
            )
        elif system == "Linux" and shutil.which("notify-send"):
            subprocess.run(
                ["notify-send", title, message],
                capture_output=True,
                timeout=5,
                check=False,
            )
        else:
            logger.debug(
                "No native notifier available on %s; skipping notification.", system
            )
    except Exception:  # pragma: no cover - notifications are best-effort
        logger.debug("Failed to send notification.", exc_info=True)


def _osascript_quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'
