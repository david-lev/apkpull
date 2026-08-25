"""Exception hierarchy for apkpull.

Every error that can be attributed to a *specific device* subclasses
:class:`DeviceError` so the orchestrator can catch a single base class,
record the failure against that device and keep processing the rest.
"""

from __future__ import annotations


class ApkPullError(Exception):
    """Base class for all apkpull errors."""


class AdbNotFoundError(ApkPullError):
    """The ``adb`` executable could not be located on ``PATH``."""


class NoDevicesFoundError(ApkPullError):
    """No devices/emulators are visible to ``adb``."""


class InvalidPackageNameError(ApkPullError):
    """The supplied package name is not syntactically valid."""


class DeviceError(ApkPullError):
    """Base class for errors scoped to a single device.

    Raising one of these inside :func:`apkpull.orchestrator.run_for_device`
    aborts that device's run without affecting the others.
    """

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class DeviceDisconnectedError(DeviceError):
    """The device went offline / unauthorized / was unplugged mid-run."""


class DeviceLockedTimeoutError(DeviceError):
    """The device stayed locked past ``AutomationConfig.unlock_timeout``.

    Without a bound here, a single locked device would hang the *entire*
    run forever: :func:`apkpull.orchestrator.run` waits on every device's
    future via ``as_completed()``, so one stuck device blocks the whole
    CLI from ever printing a summary, even though every other device may
    have long since finished.
    """


class GooglePlayUnavailableError(DeviceError):
    """The Play Store app is missing or disabled on the device."""


class UnsupportedLocaleError(DeviceError):
    """The device's system language has no known Play Store button strings."""


class PaidAppError(DeviceError):
    """The app requires payment and cannot be pulled automatically."""


class RegionRestrictedError(DeviceError):
    """The app is not available in the device's Play Store region."""


class IncompatibleDeviceError(DeviceError):
    """Play Store reports the device is not compatible with this app."""


class NotSignedInError(DeviceError):
    """No Google account is signed in on the device."""


class DeviceOfflineError(DeviceError):
    """The device itself reports it has no network connection."""


class InsufficientStorageError(DeviceError):
    """Play Store's "Not enough storage" dialog reports the device doesn't have
    room to install/update this package."""


class AppNotFoundError(DeviceError):
    """Play Store's on-device UI reports the package doesn't exist.

    Deliberately *not* raised from an unauthenticated web request anymore —
    that check runs from apkpull's host machine, whose network/region isn't
    necessarily the device's Play Store account's region, so a 404 there
    doesn't reliably mean "this device can't install it either" (a region-
    locked app is the common case). The on-device UI is the actual source of
    truth, and scoping this to one device/package means a single bad package
    name no longer aborts every other device's run.
    """


class UnrecognizedPlayStoreError(DeviceError):
    """Play Store showed a warning-triangle error banner whose text doesn't match
    any of the specifically-handled screens above (paid app, region-restricted,
    offline, incompatible device, not signed in) — raised with the banner's
    actual on-screen text so an unfamiliar error is still diagnosable, instead
    of silently polling until :class:`AutomationTimeoutError` gives up with no
    real explanation."""


class AutomationTimeoutError(DeviceError):
    """UI automation could not find an expected button/state in time."""

    def __init__(
        self, message: str, *, screenshot: str | None = None, dump: str | None = None
    ) -> None:
        super().__init__(message, retryable=True)
        self.screenshot = screenshot
        self.dump = dump


class DownloadTimeoutError(DeviceError):
    """A package's download/update did not finish within ``download_timeout``,
    even after ``download_retries`` restart attempts.

    Without a bound here, a stalled download would hang the completion-pass
    poll loop in :func:`apkpull.orchestrator.run_for_device` forever.
    """


class PullError(DeviceError):
    """Pulling apk/obb files off the device failed."""


class VerificationError(DeviceError):
    """A pulled file failed post-pull verification against device metadata."""
