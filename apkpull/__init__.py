"""apkpull: automate pulling Android apps from Google Play across one or more devices."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version

from .exceptions import (
    ApkPullError,
    AppNotFoundError,
    DeviceError,
    DeviceLockedTimeoutError,
    InvalidPackageNameError,
    NoDevicesFoundError,
)
from .models import DeviceOutcome, OutputFormat, PulledFile, RunSummary, Status
from .orchestrator import run

try:
    # `pyproject.toml`'s `[project] version` is the single source of truth — it's what ends up in
    # the installed distribution's metadata (standard PEP 621 behavior for any build backend,
    # including uv_build), so reading it back from there avoids keeping a second copy in sync here.
    __version__ = _pkg_version("apkpull")
except (
    PackageNotFoundError
):  # pragma: no cover - only when apkpull is used without being installed
    __version__ = "0.0.0+unknown"

__all__ = [
    "ApkPullError",
    "AppNotFoundError",
    "DeviceError",
    "DeviceLockedTimeoutError",
    "DeviceOutcome",
    "InvalidPackageNameError",
    "NoDevicesFoundError",
    "OutputFormat",
    "PulledFile",
    "RunSummary",
    "Status",
    "__version__",
    "run",
]
