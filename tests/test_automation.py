import dataclasses

import pytest

from apkpull.automation import AutomationConfig, KickoffResult, PlayStoreAutomator
from apkpull.device import Device
from apkpull.exceptions import (
    AppNotFoundError,
    AutomationTimeoutError,
    DeviceLockedTimeoutError,
    DeviceOfflineError,
    IncompatibleDeviceError,
    InsufficientStorageError,
    NotSignedInError,
    PaidAppError,
    RegionRestrictedError,
    UnrecognizedPlayStoreError,
)
from apkpull.locales import LOCALES
from apkpull.models import Status
from apkpull.progress import Stage

from .helpers import FakeAdb, make_dump, make_icon_dump

PLAY_FOCUSED = "mCurrentFocus=Window{d2b u0 com.android.vending/.MainActivity}"
INSTALL = make_dump(("Install", (0, 0, 100, 50)))
CANCEL = make_dump(("Cancel", (0, 0, 100, 50)))
OPEN = make_dump(("Open", (0, 0, 100, 50)))
UPDATE = make_dump(("Update", (0, 0, 100, 50)))


def build(
    package="com.app",
    *,
    pm_path=None,
    focused_window=PLAY_FOCUSED,
    dumps=None,
    max_rounds=3,
):
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = focused_window
    if pm_path is not None:
        adb.shell_responses[f"pm path {package}"] = pm_path
    if dumps is not None:
        adb.shell_responses[
            "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
            "cat /sdcard/window_dump.xml"
        ] = dumps
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0, max_rounds=max_rounds)
    )
    return automator, device, adb


# -- start_install --------------------------------------------------------


def test_start_install_taps_and_reports_tracking_needed():
    """Typical happy path: tap Install, the download is now running in the
    background — start_install's job is done, completion is tracked elsewhere.
    """
    automator, _device, adb = build(pm_path="", dumps=[INSTALL])
    assert automator.start_install("com.app") == KickoffResult(needs_tracking=True)
    assert "input tap 50 25" in adb.shell_log  # center of the Install button


def test_start_install_skips_tap_when_already_downloading():
    automator, _device, adb = build(dumps=[CANCEL])
    assert automator.start_install("com.app") == KickoffResult(needs_tracking=True)
    assert not any(cmd.startswith("input tap") for cmd in adb.shell_log)


def test_start_install_reports_done_when_install_finishes_instantly():
    """Regression test: on a fast connection the app can finish installing
    right after the tap, without ever showing "Cancel" — start_install must
    notice via pm path and report needs_tracking=False instead of assuming a
    download is still running.
    """
    automator, _device, _adb = build(
        pm_path="package:/data/app/base.apk\n", dumps=[INSTALL]
    )
    assert automator.start_install("com.app") == KickoffResult(
        needs_tracking=False, status=Status.INSTALLED
    )


@pytest.mark.parametrize(
    "button_text, exc_type",
    [
        (LOCALES["en"].hardware_incompatible, IncompatibleDeviceError),
        (LOCALES["en"].country_restricted, RegionRestrictedError),
        (LOCALES["en"].offline, DeviceOfflineError),
        (LOCALES["en"].sign_in, NotSignedInError),
        (LOCALES["en"].not_found, AppNotFoundError),
        ("$4.99", PaidAppError),
    ],
)
def test_start_install_raises_on_error_screens(button_text, exc_type):
    dump = make_dump((button_text, (0, 0, 100, 50)))
    automator, _device, _adb = build(dumps=[dump])
    with pytest.raises(exc_type):
        automator.start_install("com.app")


def test_start_install_ignores_sign_in_text_when_locale_lacks_it():
    """sign_in is optional (unlike the tap-target fields above it) -- a
    ButtonSet without it confirmed yet must not crash, or misfire, on a dump
    that happens to contain literally "Sign in": the check has to be skipped
    entirely, not just fail to match."""
    buttons_without_sign_in = dataclasses.replace(LOCALES["en"], sign_in=None)
    dump = make_dump(("Sign in", (0, 0, 100, 50)))
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = PLAY_FOCUSED
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [dump]
    automator = PlayStoreAutomator(
        Device(adb, "fake-1"),
        buttons_without_sign_in,
        AutomationConfig(poll_interval=0, max_rounds=1),
    )
    with pytest.raises(AutomationTimeoutError):
        automator.start_install("com.app")


def test_start_install_raises_generic_error_for_unrecognized_warning_banner():
    """A red-warning-triangle banner apkpull has no specific rule for must still
    surface its actual on-screen text immediately, instead of silently polling
    until AutomationTimeoutError gives up with no real explanation."""
    dump = make_icon_dump("Warning", "Some new Play Store error we've never seen.")
    automator, _device, _adb = build(dumps=[dump])
    with pytest.raises(UnrecognizedPlayStoreError, match="Some new Play Store error"):
        automator.start_install("com.app")


def test_start_install_prefers_specific_error_over_generic_warning_banner():
    """A screen matching one of the specifically-handled banners (e.g. region
    restriction) must raise its specific type, not fall through to the generic
    UnrecognizedPlayStoreError catch-all — even though Play Store pairs that
    screen with the same warning-triangle icon too."""
    dump = make_icon_dump("Warning", LOCALES["en"].country_restricted)
    automator, _device, _adb = build(dumps=[dump])
    with pytest.raises(RegionRestrictedError):
        automator.start_install("com.app")


NOT_ENOUGH_STORAGE = make_dump(("Not enough storage", (0, 0, 100, 50)))


def test_start_install_raises_when_post_tap_dialog_shows_insufficient_storage():
    """Confirmed hands-on: Play Store's "Not enough storage" dialog only
    appears *after* tapping Install, never on the details page itself -- it's
    the post-tap dump in _tap_until_download_starts that actually catches it.
    Without that dump, is_installed() would just report False forever and the
    caller would assume a download silently started in the background."""
    automator, _device, adb = build(dumps=[INSTALL, NOT_ENOUGH_STORAGE])
    with pytest.raises(InsufficientStorageError):
        automator.start_install("com.app")
    assert "input tap 50 25" in adb.shell_log


def test_start_install_raises_device_offline_for_no_internet_dialog():
    """Confirmed hands-on: this full-page "Something went wrong" / "No
    internet connection..." state is a *different* screen from the themed
    "You're offline" page `offline` already catches — both must map to the
    same DeviceOfflineError, since it's the same underlying condition."""
    dump = make_dump((LOCALES["en"].no_internet_dialog, (0, 0, 100, 50)))
    automator, _device, _adb = build(dumps=[dump])
    with pytest.raises(DeviceOfflineError):
        automator.start_install("com.app")


def test_start_install_times_out_and_captures_error(tmp_path):
    automator, _device, _adb = build(
        dumps=[make_dump(("nothing useful", (0, 0, 1, 1)))], max_rounds=2
    )
    automator.config.logs_dir = tmp_path / "logs"
    with pytest.raises(AutomationTimeoutError) as excinfo:
        automator.start_install("com.app")
    assert excinfo.value.screenshot and excinfo.value.dump
    assert (tmp_path / "logs").is_dir()
    assert any((tmp_path / "logs").iterdir())


def test_start_install_relaunches_when_navigated_away_from_play_store():
    automator, _device, adb = build(
        focused_window="mCurrentFocus=Window{d2b u0 com.android.settings/.Main}",
        pm_path="package:/data/app/base.apk\n",
        dumps=[make_dump(("nothing", (0, 0, 1, 1))), CANCEL],
        max_rounds=5,
    )
    automator.start_install("com.app")
    launch_cmds = [c for c in adb.shell_log if c.startswith("am start")]
    assert len(launch_cmds) >= 2  # initial launch + at least one relaunch


# -- start_update -----------------------------------------------------------


def test_start_update_already_up_to_date():
    automator, _device, adb = build(dumps=[OPEN])
    assert automator.start_update("com.app") == KickoffResult(
        needs_tracking=False, status=Status.ALREADY_UP_TO_DATE
    )
    assert not any(cmd.startswith("input tap") for cmd in adb.shell_log)


def test_start_update_taps_and_reports_tracking_needed():
    versions = iter([100, 100])
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = PLAY_FOCUSED
    adb.shell_responses["dumpsys package com.app"] = lambda: (
        f"versionCode={next(versions, 100)} versionName=1.0"
    )
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [UPDATE]
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0, max_rounds=5)
    )
    assert automator.start_update("com.app") == KickoffResult(
        needs_tracking=True, baseline_version_code=100
    )


def test_start_update_reports_done_when_update_finishes_instantly():
    versions = iter([100, 101])
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = PLAY_FOCUSED
    adb.shell_responses["dumpsys package com.app"] = lambda: (
        f"versionCode={next(versions, 101)} versionName=1.0"
    )
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [UPDATE]
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0, max_rounds=5)
    )
    assert automator.start_update("com.app") == KickoffResult(
        needs_tracking=False, status=Status.UPDATED
    )


def test_start_update_already_updating_skips_tap():
    versions = iter([100, 100, 101])
    automator, _device, adb = build(dumps=[CANCEL])
    adb.shell_responses["dumpsys package com.app"] = lambda: (
        f"versionCode={next(versions, 101)} versionName=1.0"
    )
    assert automator.start_update("com.app") == KickoffResult(
        needs_tracking=True, baseline_version_code=100
    )
    assert not any(cmd.startswith("input tap") for cmd in adb.shell_log)


# -- wait_for_unlock ----------------------------------------------------------


def test_wait_for_unlock_returns_immediately_when_unlocked():
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = (
        "mShowingDream=false mDreamingLockscreen=false"
    )
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0)
    )
    automator.wait_for_unlock()  # must not raise / hang


def test_wait_for_unlock_polls_until_unlocked():
    states = iter(
        [
            "mShowingDream=true",
            "mShowingDream=true",
            "mShowingDream=false mDreamingLockscreen=false",
        ]
    )
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = lambda: next(states)
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0, unlock_poll_interval=0)
    )
    automator.wait_for_unlock()


def test_wait_for_unlock_raises_after_timeout():
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = "mShowingDream=true"  # never unlocks
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device,
        LOCALES["en"],
        AutomationConfig(poll_interval=0, unlock_poll_interval=0, unlock_timeout=0.05),
    )
    with pytest.raises(DeviceLockedTimeoutError):
        automator.wait_for_unlock()


def test_wait_for_unlock_zero_timeout_waits_indefinitely():
    states = iter(
        ["mShowingDream=true"] * 5 + ["mShowingDream=false mDreamingLockscreen=false"]
    )
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = lambda: next(states)
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device,
        LOCALES["en"],
        AutomationConfig(poll_interval=0, unlock_poll_interval=0, unlock_timeout=0),
    )
    automator.wait_for_unlock()  # must not raise despite outlasting a would-be short timeout


def test_wait_for_unlock_reports_locked_when_actually_locked():
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = "mShowingDream=true"  # never unlocks
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device,
        LOCALES["en"],
        AutomationConfig(poll_interval=0, unlock_poll_interval=0, unlock_timeout=0.05),
    )
    reported: list[tuple[Stage, str]] = []
    with pytest.raises(DeviceLockedTimeoutError):
        automator.wait_for_unlock(
            report=lambda stage, detail: reported.append((stage, detail))
        )
    assert reported == [(Stage.LOCKED, "")]


def test_wait_for_unlock_does_not_report_when_already_unlocked():
    """report must only fire for a genuinely locked device -- not on every
    call, or every device with a report callback would show as briefly
    "locked" even when it never was."""
    adb = FakeAdb()
    adb.shell_responses["dumpsys window"] = (
        "mShowingDream=false mDreamingLockscreen=false"
    )
    device = Device(adb, "fake-1")
    automator = PlayStoreAutomator(
        device, LOCALES["en"], AutomationConfig(poll_interval=0)
    )
    reported: list[tuple[Stage, str]] = []
    automator.wait_for_unlock(
        report=lambda stage, detail: reported.append((stage, detail))
    )
    assert reported == []
