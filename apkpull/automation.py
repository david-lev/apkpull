"""Drives the Google Play Store app's UI to install or update a package.

Play Store exposes no stable resource-ids on its buttons (confirmed by
dumping the live UI tree on a real emulator — every text node's
``resource-id`` attribute is empty), so — like the original bash script —
this automates by finding buttons via their (localized) text and tapping
the resulting screen coordinates. See :mod:`apkpull.locales` for the string
tables and :mod:`apkpull.uidump` for the dump-parsing primitives.
"""

from __future__ import annotations

import logging
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import uidump
from .device import Device
from .exceptions import (
    AppNotFoundError,
    AutomationTimeoutError,
    DeviceDisconnectedError,
    DeviceLockedTimeoutError,
    DeviceOfflineError,
    IncompatibleDeviceError,
    InsufficientStorageError,
    NotSignedInError,
    PaidAppError,
    RegionRestrictedError,
    UnrecognizedPlayStoreError,
)
from .locales import PAID_APP_CURRENCY_SYMBOLS, ButtonSet
from .models import Status
from .progress import DeviceReporter, Stage


@dataclass(frozen=True, slots=True)
class KickoffResult:
    """Result of starting (not waiting out) an install/update.

    ``needs_tracking`` is ``False`` when there's nothing left to do — either
    the app was already up to date, or it finished installing/updating
    before we even finished tapping (rare, but real on a fast connection).
    Otherwise the caller should poll ``device.is_installed``/``version_code``
    (for installs/updates respectively) until it resolves.
    """

    needs_tracking: bool
    status: Status | None = None
    baseline_version_code: int | None = None


logger = logging.getLogger("apkpull.automation")

GOOGLE_PLAY_PACKAGE = "com.android.vending"


@dataclass(slots=True)
class AutomationConfig:
    max_rounds: int = 5
    """Consecutive polls with no recognizable UI state before giving up."""
    poll_interval: float = 1.0
    unlock_poll_interval: float = 1.0
    unlock_timeout: float = 300.0
    """Give up waiting for a locked device after this many seconds. ``0`` waits forever."""
    logs_dir: Path = Path(tempfile.gettempdir()) / "apkpull_logs"


class PlayStoreAutomator:
    """Stateful helper bound to one :class:`~apkpull.device.Device`."""

    def __init__(
        self, device: Device, buttons: ButtonSet, config: AutomationConfig | None = None
    ) -> None:
        self.device = device
        self.buttons = buttons
        self.config = config or AutomationConfig()

    # -- public flows ---------------------------------------------------------

    def launch(self, package: str) -> None:
        url = f"https://play.google.com/store/apps/details?id={package}"
        self.device.launch_url(url, GOOGLE_PLAY_PACKAGE)

    def wait_for_unlock(self, report: DeviceReporter | None = None) -> None:
        if self.device.is_unlocked():
            return
        logger.warning(
            "[%s] Device is locked. Waiting for it to be unlocked...", self.device.label
        )
        if report:
            report(Stage.LOCKED, "")
        start = time.monotonic()
        while not self.device.is_unlocked():
            self.device.ensure_connected()
            elapsed = time.monotonic() - start
            if self.config.unlock_timeout and elapsed >= self.config.unlock_timeout:
                raise DeviceLockedTimeoutError(
                    f"Device stayed locked for over {self.config.unlock_timeout:.0f}s; giving up."
                )
            time.sleep(self.config.unlock_poll_interval)
        logger.info("[%s] Device unlocked.", self.device.label)

    def start_install(self, package: str) -> KickoffResult:
        """Kick off the "not installed yet" flow — does not wait for it to finish."""
        b = self.buttons
        logger.info(
            "[%s] Launching Google Play to %s's page.", self.device.label, package
        )
        self.launch(package)

        install_coords, _nodes = self._wait_for_button(
            package,
            button_text=b.install,
            also_break_on=(b.cancel,),
        )
        if install_coords is None:
            # `_wait_for_button`'s only other exit condition is `also_break_on`, so
            # `install_coords is None` here always means the "Cancel" button matched.
            logger.info("[%s] %s is already downloading.", self.device.label, package)
            return KickoffResult(needs_tracking=True)

        self._tap_until_download_starts(package, install_coords)
        if self.device.is_installed(package):
            # Fast connection: it can finish installing before we ever see "Cancel".
            logger.info("[%s] %s installed successfully!", self.device.label, package)
            return KickoffResult(needs_tracking=False, status=Status.INSTALLED)

        logger.info("[%s] %s: download started.", self.device.label, package)
        return KickoffResult(needs_tracking=True)

    def start_update(self, package: str) -> KickoffResult:
        """Kick off the "already installed, check for update" flow."""
        b = self.buttons
        self.launch(package)

        try:
            update_coords, nodes = self._wait_for_button(
                package,
                button_text=b.update,
                also_break_on=(b.cancel, b.open, b.play, b.uninstall, b.deactivate),
            )
        except AutomationTimeoutError:
            logger.warning(
                "[%s] Could not determine update status for %s.",
                self.device.label,
                package,
            )
            return KickoffResult(needs_tracking=False, status=Status.ALREADY_UP_TO_DATE)

        # `update_coords is None` is ambiguous by itself: `also_break_on` matches
        # either "already up to date" (Open/Play/Uninstall visible, no Update
        # button) or "already updating" (Cancel visible). Only the latter should
        # fall through to tracking the update.
        if update_coords is None and not uidump.contains_text(nodes, b.cancel):
            logger.info("[%s] %s is already up to date.", self.device.label, package)
            return KickoffResult(needs_tracking=False, status=Status.ALREADY_UP_TO_DATE)

        before = self.device.version_code(package)
        if before is None:
            raise AutomationTimeoutError(
                f"Could not read {package}'s current version code from the device."
            )
        if update_coords is None:
            logger.info("[%s] %s is already updating.", self.device.label, package)
            return KickoffResult(needs_tracking=True, baseline_version_code=before)

        self._tap_until_download_starts(package, update_coords)
        if self.device.version_code(package) != before:
            logger.info("[%s] %s updated successfully!", self.device.label, package)
            return KickoffResult(needs_tracking=False, status=Status.UPDATED)

        logger.info("[%s] %s: update started.", self.device.label, package)
        return KickoffResult(needs_tracking=True, baseline_version_code=before)

    # -- internals --------------------------------------------------------------

    def _check_error_screens(self, nodes: list[uidump.UiNode]) -> None:
        """``nodes`` is one dump parsed once by the caller (``_wait_for_button``/
        ``_tap_until_download_starts``) — every check below queries that same
        parsed list rather than re-scanning the raw XML itself, since a single
        poll tick used to trigger up to a dozen-plus independent re-parses of
        the same dump here."""
        b = self.buttons
        if b.not_found and uidump.contains_text(nodes, b.not_found):
            raise AppNotFoundError(b.not_found)
        if b.insufficient_storage and uidump.contains_text(
            nodes, b.insufficient_storage
        ):
            raise InsufficientStorageError(b.insufficient_storage)
        if uidump.contains_text(nodes, b.hardware_incompatible):
            raise IncompatibleDeviceError(b.hardware_incompatible)
        if uidump.contains_text(nodes, b.country_restricted):
            raise RegionRestrictedError(b.country_restricted)
        if uidump.contains_text(nodes, b.offline):
            raise DeviceOfflineError(f"{b.offline}.")
        if b.no_internet_dialog and uidump.contains_text(nodes, b.no_internet_dialog):
            raise DeviceOfflineError(b.no_internet_dialog)
        if uidump.contains_any_currency_amount(nodes, PAID_APP_CURRENCY_SYMBOLS):
            raise PaidAppError("This app is paid.")
        if b.sign_in and uidump.contains_text(nodes, b.sign_in):
            raise NotSignedInError("You must be logged in to a Google account.")
        if b.warning_icon:
            message = uidump.find_text_near_content_desc(nodes, b.warning_icon)
            if message:
                raise UnrecognizedPlayStoreError(f"Google Play showed: {message!r}")

    def _wait_for_button(
        self, package: str, *, button_text: str, also_break_on: tuple[str, ...]
    ) -> tuple[tuple[int, int] | None, list[uidump.UiNode]]:
        """Poll until ``button_text`` (or any text in ``also_break_on``) appears.

        Returns ``(coords, nodes)`` — ``coords`` is ``None`` when the match was
        one of ``also_break_on`` rather than the target button itself; ``nodes``
        is the winning poll's dump, already parsed once, for the caller to
        query further without re-parsing.
        """
        rounds = 0
        while True:
            self.device.ensure_connected()
            dump = self.device.dump_ui()
            nodes = uidump.parse(dump)
            self._check_error_screens(nodes)

            coords = uidump.find_button(nodes, button_text)
            if coords is not None:
                return coords, nodes
            if any(uidump.contains_text(nodes, t) for t in also_break_on):
                return None, nodes

            if not self.device.is_foreground_package(GOOGLE_PLAY_PACKAGE):
                logger.info("[%s] Left Google Play, relaunching...", self.device.label)
                self.launch(package)

            rounds += 1
            if rounds >= self.config.max_rounds:
                screenshot, dump_path = self._capture_error(dump)
                raise AutomationTimeoutError(
                    f"An unknown error occurred waiting for '{button_text}'.",
                    screenshot=str(screenshot),
                    dump=str(dump_path),
                )
            time.sleep(self.config.poll_interval)

    def _tap_until_download_starts(self, package: str, coords: tuple[int, int]) -> None:
        """Tap ``coords`` (the install/update button), then take one more look to
        catch an immediate post-tap failure (confirmed hands-on: Play Store's
        "Not enough storage" dialog appears only *after* this tap, never on the
        details page itself — without this check the caller would just assume a
        download silently started in the background and poll adb forever for an
        install that will never happen) or see whether the "Cancel" button
        appeared (download started) or the device wandered off Google Play
        mid-tap. Either way, our job here is just to *start* the download; the
        caller tracks completion over adb.
        """
        self.device.ensure_connected()
        self.device.tap(*coords)
        self._check_error_screens(uidump.parse(self.device.dump_ui()))
        if not self.device.is_foreground_package(GOOGLE_PLAY_PACKAGE):
            logger.info("[%s] Left Google Play, relaunching...", self.device.label)
            self.launch(package)

    def _capture_error(self, dump: str) -> tuple[Path, Path]:
        self.config.logs_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y%m%d_%H%M%S")
        info = self.device.info()
        # device_id in the filename (not just model/lang) since two devices
        # erroring in the same second with the same model+lang -- exactly the
        # "duplicate-looking devices" scenario apkpull itself warns about --
        # would otherwise silently overwrite each other's diagnostic files.
        base = self.config.logs_dir / (
            f"{stamp}_{info.model}_{info.lang}_{self.device.device_id}".replace(
                " ", "_"
            ).replace(":", "_")
        )
        dump_path = base.with_suffix(".xml")
        screenshot_path = base.with_suffix(".png")
        dump_path.write_text(dump, encoding="utf-8", errors="replace")
        try:
            self.device.adb.exec_out_to_file(
                self.device.device_id, "screencap -p", screenshot_path
            )
        except DeviceDisconnectedError:
            logger.debug(
                "Could not capture screenshot; device disconnected mid-capture."
            )
        logger.warning(
            "[%s] LOG: saved screenshot/dump to %s / %s",
            self.device.label,
            screenshot_path,
            dump_path,
        )
        return screenshot_path, dump_path
