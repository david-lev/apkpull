"""apkpull: automate pulling Android apps from Google Play across one or more devices."""

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

__version__ = "2.0.0"

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
