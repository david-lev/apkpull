"""Thin, typed wrapper around the ``adb`` command-line tool.

Nothing in this module knows anything about Google Play or apk pulling —
it is a general purpose adb client so it can be unit tested by mocking
``subprocess.run`` alone, and reused outside apkpull if needed.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .exceptions import AdbNotFoundError, DeviceDisconnectedError

logger = logging.getLogger("apkpull.adb")

DEFAULT_TIMEOUT = 30.0


@dataclass(frozen=True, slots=True)
class AdbResult:
    args: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


class AdbClient:
    """Runs ``adb`` subprocesses. One instance is shared across all devices."""

    def __init__(self, adb_path: str | None = None) -> None:
        resolved = adb_path or shutil.which("adb")
        if not resolved:
            raise AdbNotFoundError(
                "Unable to find `adb`. Install Android platform-tools or add it to PATH."
            )
        self.adb_path = resolved
        logger.debug("Using adb at %s", self.adb_path)

    # -- low level -----------------------------------------------------

    def _run(
        self, args: list[str], *, timeout: float = DEFAULT_TIMEOUT, check: bool = False
    ) -> AdbResult:
        cmd = [self.adb_path, *args]
        logger.debug("$ %s", " ".join(cmd))
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise DeviceDisconnectedError(
                f"adb command timed out: {' '.join(cmd)}"
            ) from exc
        result = AdbResult(
            args=cmd, returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr
        )
        if not result.ok:
            logger.debug("adb exited %s: %s", result.returncode, result.stderr.strip())
        if check and not result.ok:
            raise DeviceDisconnectedError(
                result.stderr.strip() or f"adb command failed: {' '.join(cmd)}"
            )
        return result

    # -- device discovery ------------------------------------------------

    def list_devices(self) -> list[str]:
        """Return device ids currently in the ``device`` (ready) state."""
        result = self._run(["devices"])
        devices = []
        for line in result.stdout.splitlines()[1:]:
            line = line.strip()
            if not line or "\t" not in line:
                continue
            device_id, state = line.split("\t", 1)
            if state.strip() == "device":
                devices.append(device_id)
        return devices

    def get_state(self, device_id: str) -> str:
        result = self._run(["-s", device_id, "get-state"], timeout=5)
        if not result.ok:
            return (
                (result.stderr or "unknown")
                .strip()
                .replace("error: device ", "")
                .rstrip(".")
            )
        return result.stdout.strip()

    def is_connected(self, device_id: str) -> bool:
        return self.get_state(device_id) == "device"

    # -- shell -----------------------------------------------------------

    def shell(
        self,
        device_id: str,
        command: str,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        check: bool = True,
    ) -> str:
        result = self._run(
            ["-s", device_id, "shell", command], timeout=timeout, check=check
        )
        return result.stdout

    # -- file transfer -----------------------------------------------------

    def pull(
        self,
        device_id: str,
        remote_path: str,
        local_path: Path,
        *,
        timeout: float = 300.0,
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        result = self._run(
            ["-s", device_id, "pull", remote_path, str(local_path)], timeout=timeout
        )
        if not result.ok:
            raise DeviceDisconnectedError(
                f"Failed to pull {remote_path}: {result.stderr.strip()}"
            )

    def exec_out_to_file(
        self, device_id: str, command: str, local_path: Path, *, timeout: float = 30.0
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        cmd = [self.adb_path, "-s", device_id, "exec-out", *command.split()]
        try:
            with open(local_path, "wb") as fh:
                proc = subprocess.run(
                    cmd, stdout=fh, stderr=subprocess.PIPE, timeout=timeout, check=False
                )
        except subprocess.TimeoutExpired as exc:
            raise DeviceDisconnectedError(f"exec-out timed out: {command}") from exc
        if proc.returncode != 0:
            raise DeviceDisconnectedError(
                f"exec-out failed: {proc.stderr.decode(errors='replace').strip()}"
            )
