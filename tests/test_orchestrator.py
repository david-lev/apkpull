import urllib.error
import zipfile
from concurrent.futures import Future
from unittest.mock import MagicMock, patch

import pytest

from apkpull.automation import AutomationConfig
from apkpull.exceptions import (
    InvalidPackageNameError,
    NoDevicesFoundError,
    PullError,
    VerificationError,
)
from apkpull.models import DeviceInfo, DeviceOutcome, OutputFormat, Status
from apkpull.orchestrator import (
    _drain_batch,
    _merge_pending_contributions,
    _PendingContribution,
    _warn_about_duplicate_devices,
    check_package_exists,
    run,
    run_for_device,
    validate_package_name,
)
from apkpull.progress import ProgressEvent, Stage
from apkpull.puller import RawPull, target_path

from .helpers import (
    POLITEDROID_BYTES,
    TEST_DEBUG_BYTES,
    FakeAdb,
    configure_apks_create,
    make_dump,
)

OPEN = make_dump(("Open", (0, 0, 100, 50)))


def _run_for_device_and_merge(adb, device_id, packages, dest_root, **kwargs):
    """run_for_device() alone no longer finishes building bundles for
    devices that need one -- real merging now happens in run()'s post-drain
    merge phase (see _merge_pending_contributions). This chains the two
    exactly as run() does, without a second device or any threading, for
    tests that want the old all-in-one result.

    verify/strict_verify/full_manifest only matter to the merge step now
    (run_for_device no longer accepts them at all -- building/verifying
    moved entirely to _merge_pending_contributions), so they're popped out
    before forwarding the rest of kwargs to run_for_device.
    """
    verify = kwargs.pop("verify", True)
    strict_verify = kwargs.pop("strict_verify", False)
    full_manifest = kwargs.pop("full_manifest", False)
    outcomes, pending = run_for_device(adb, device_id, packages, dest_root, **kwargs)
    _merge_pending_contributions(
        outcomes,
        pending,
        dest_root,
        verify=verify,
        strict_verify=strict_verify,
        full_manifest=full_manifest,
        output_format=kwargs.get("output_format", OutputFormat.APKS),
        notify_enabled=kwargs.get("notify_enabled", False),
        on_progress=kwargs.get("on_progress"),
    )
    return outcomes


def _make_contribution(tmp_path, device_id, package, version_code, *, splits):
    """Build a (_PendingContribution) directly, bypassing FakeAdb/
    Puller.pull_raw -- these are for run()-level tests exercising the
    merge-phase *wiring* (grouping, warnings, staging cleanup), not the
    device-pull mechanics themselves (already covered in test_puller.py).

    base_path/split_paths get real fixture apk bytes (not literal placeholder
    text) since the merge phase feeds them into a real ApksFile.create() call
    -- callers wrap the eventual run()/build_merged_bundle() call in
    force_splits(where=...) so these split filenames are actually treated as
    splits rather than competing base apks."""
    staging = tmp_path / ".apkpull-staging" / f"{package}-{version_code}" / device_id
    staging.mkdir(parents=True)
    base_path = staging / "base.apk"
    base_path.write_bytes(POLITEDROID_BYTES)
    split_paths = []
    for name in splits:
        split_path = staging / name
        split_path.write_bytes(TEST_DEBUG_BYTES)
        split_paths.append(split_path)
    target = target_path(tmp_path, package, version_code, OutputFormat.APKS)
    raw = RawPull(
        device_id=device_id,
        package=package,
        version_code=version_code,
        version_name="1.0",
        target=target,
        already_built=False,
        base_path=base_path,
        split_paths=split_paths,
        obb_paths=[],
        staging_dir=staging,
    )
    outcome = DeviceOutcome(
        device=DeviceInfo(device_id=device_id, model=device_id),
        package=package,
        status=Status.INSTALLED,
        pulled_files=[],
        version_code=version_code,
        version_name="1.0",
        destination=target,
    )
    return _PendingContribution(raw=raw, outcome=outcome)


# -- validate_package_name ------------------------------------------------


@pytest.mark.parametrize("package", ["com.whatsapp", "com.a.b_c.d", "a.b"])
def test_validate_package_name_accepts_valid(package):
    validate_package_name(package)  # must not raise


@pytest.mark.parametrize("package", ["", "com", "1com.app", "com..app", "com.app!"])
def test_validate_package_name_rejects_invalid(package):
    with pytest.raises(InvalidPackageNameError):
        validate_package_name(package)


# -- check_package_exists --------------------------------------------------


def test_check_package_exists_warns_but_does_not_raise_on_404(caplog):
    """A 404 from Play Store's unauthenticated *website* doesn't reliably mean
    the package can't be installed on the actual target device — a region-
    locked app (e.g. a bank's) can 404 from one network/country while still
    installing fine on a device with the right account/region. This must
    never hard-abort the run; the on-device automation is what actually knows."""
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.HTTPError("u", 404, "not found", {}, None),
        ),
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        check_package_exists("com.nonexistent")  # must not raise

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "com.nonexistent" in warnings[0]


def test_check_package_exists_ignores_network_errors():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timeout")):
        check_package_exists("com.whatsapp")  # must not raise


def test_check_package_exists_ok_on_200():
    response = MagicMock()
    response.status = 200
    response.__enter__.return_value = response
    with patch("urllib.request.urlopen", return_value=response):
        check_package_exists("com.whatsapp")  # must not raise


# -- run_for_device --------------------------------------------------------


def _adb_with_device(*, lang="en-US", sdk=None, play_installed=True, pkg_pm_path=""):
    adb = FakeAdb()
    sdk_prop = f"[ro.build.version.sdk]: [{sdk}]\n" if sdk is not None else ""
    adb.shell_responses["getprop"] = (
        f"[ro.product.model]: [Pixel]\n[ro.product.cpu.abi]: [arm64-v8a]\n"
        f"[persist.sys.locale]: [{lang}]\n{sdk_prop}"
    )
    adb.shell_responses["pm path com.android.vending"] = (
        "package:/data/app/vending/base.apk\n" if play_installed else ""
    )
    adb.shell_responses["pm list packages -d"] = ""
    # Both is_unlocked() and focused_window() read `dumpsys window`, so one scripted
    # response has to carry both facts.
    adb.shell_responses["dumpsys window"] = (
        "mShowingDream=false mDreamingLockscreen=false\n"
        "mCurrentFocus=Window{d2b u0 com.android.vending/.Main}"
    )
    adb.shell_responses["settings get global stay_on_while_plugged_in"] = "0\n"
    adb.shell_responses["pm path com.app"] = pkg_pm_path
    adb.shell_responses["cmd locale get-app-locales com.android.vending --user 0"] = (
        "Locales for com.android.vending for user 0 are []"
    )
    return adb


def test_run_for_device_reports_error_when_play_store_missing():
    adb = _adb_with_device(play_installed=False)
    outcomes, _pending = run_for_device(adb, "fake-1", ["com.app"], None)
    assert len(outcomes) == 1
    assert outcomes[0].status == Status.ERROR
    assert "Google Play" in outcomes[0].error


def test_run_for_device_reports_error_stage_on_failure():
    """error_outcome() is the one path that can fire before info()/report()
    exist at all (e.g. Google Play missing, checked before anything else) --
    it must still emit an ERROR progress event using device_id as the
    fallback label, not crash for lack of a resolved DeviceInfo."""
    adb = _adb_with_device(play_installed=False)
    events: list[ProgressEvent] = []
    run_for_device(adb, "fake-1", ["com.app"], None, on_progress=events.append)
    assert [e.stage for e in events] == [Stage.CONNECTING, Stage.ERROR]
    assert events[-1].device_id == "fake-1"
    assert events[0].package == "com.app"


def test_run_for_device_reports_error_for_unsupported_locale():
    adb = _adb_with_device(lang="de-DE")
    outcomes, _pending = run_for_device(adb, "fake-1", ["com.app"], None)
    assert len(outcomes) == 1
    assert outcomes[0].status == Status.ERROR
    assert "language" in outcomes[0].error


def test_force_locale_overrides_an_unsupported_device_language():
    """A device whose own language isn't supported would normally hard-fail
    (see the test above) -- force_locale should let it through instead by
    overriding just Google Play's locale, on a device new enough to support
    per-app locale overrides (Android 13+ / API 33). No packages requested,
    so this exercises only the device-setup phase (where the override lives)
    without needing to fake a full Play Store automation flow too."""
    adb = _adb_with_device(lang="de-DE", sdk=34)
    outcomes, _pending = run_for_device(adb, "fake-1", [], None, force_locale="en")
    assert outcomes == []  # no UnsupportedLocaleError -- setup completed cleanly
    assert (
        "cmd locale set-app-locales com.android.vending --user 0 --locales en"
        in adb.shell_log
    )


def test_force_locale_reverts_google_plays_locale_afterward():
    adb = _adb_with_device(lang="de-DE", sdk=34)
    run_for_device(adb, "fake-1", [], None, force_locale="en")
    set_locale_calls = [
        c
        for c in adb.shell_log
        if c.startswith("cmd locale set-app-locales com.android.vending")
    ]
    assert set_locale_calls == [
        "cmd locale set-app-locales com.android.vending --user 0 --locales en",
        "cmd locale set-app-locales com.android.vending --user 0 --locales ",
    ]
    assert adb.shell_log.count("am force-stop com.android.vending") == 2


def test_force_locale_is_skipped_on_devices_too_old_to_support_it():
    """No API level reported (or below 33) -- force_locale must not be applied
    at all, and the device falls back to its own (here unsupported) language,
    exactly as if force_locale had never been passed."""
    adb = _adb_with_device(lang="de-DE", sdk=30)
    outcomes, _pending = run_for_device(
        adb, "fake-1", ["com.app"], None, force_locale="en"
    )
    assert len(outcomes) == 1
    assert outcomes[0].status == Status.ERROR
    assert "language" in outcomes[0].error
    assert not any(c.startswith("cmd locale set-app-locales") for c in adb.shell_log)


def test_run_rejects_an_unsupported_force_locale_value(tmp_path):
    """Validated up front, before any adb/device work starts -- a bad value
    (typo, unsupported language) shouldn't cost a real adb round-trip to
    discover."""
    with pytest.raises(Exception, match="force-locale"):
        run(["com.app"], tmp_path, skip_existence_check=True, force_locale="xx")


def test_run_for_device_reports_error_per_package_when_setup_fails():
    """A device-level failure (no Play Store here) applies to every requested
    package, not just the first — each still gets its own outcome."""
    adb = _adb_with_device(play_installed=False)
    outcomes, _pending = run_for_device(adb, "fake-1", ["com.app", "com.other"], None)
    assert {o.package for o in outcomes} == {"com.app", "com.other"}
    assert all(o.status == Status.ERROR for o in outcomes)


def test_run_for_device_pulls_and_verifies_already_up_to_date_app(tmp_path):
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [OPEN]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    # Puller parses the pulled base apk with ApkFile before zipping it into a bundle, and
    # then re-opens the finished bundle with ApksFile to verify it — FakeAdb.pull() writes
    # placeholder bytes rather than a real apk, so both apkfile entry points are mocked here.
    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34

        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {
            "package_name": "com.app",
            "version_code": 5,
        }
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(adb, "fake-1", ["com.app"], tmp_path)

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ok, outcome.error
    assert outcome.status == Status.ALREADY_UP_TO_DATE
    assert outcome.pulled_files[0].verified is True
    assert outcome.pulled_files[0].name == "com.app-5.apks"
    assert outcome.destination == tmp_path / "com.app-5.apks"
    assert (tmp_path / "com.app-5.manifest.json").is_file()


def test_run_for_device_reports_progress_through_the_full_pipeline(tmp_path):
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [OPEN]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    events: list[ProgressEvent] = []

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        _run_for_device_and_merge(
            adb, "fake-1", ["com.app"], tmp_path, on_progress=events.append
        )

    stages = [e.stage for e in events]
    # CONNECTING (device-level) -> OPENING_PLAY_STORE (kickoff) -> PULLING
    # (per file, from Puller) -> MERGING (this device's raw pull is done,
    # waiting on its (package, version_code) group -- trivially itself
    # here, since it's the only device) -> PACKAGING -> VERIFYING
    # (verify=True by default) -> DONE, the last two fired from the merge
    # phase once the group resolves. Note no DOWNLOADING here: this package
    # resolves immediately (already up to date), so it's never tracked as
    # an in-flight download.
    assert stages[0] == Stage.CONNECTING
    assert stages[1] == Stage.OPENING_PLAY_STORE
    assert Stage.PULLING in stages
    assert Stage.MERGING in stages
    assert Stage.PACKAGING in stages
    assert stages[-2:] == [Stage.VERIFYING, Stage.DONE]
    assert all(e.device_id == "fake-1" for e in events)
    assert all(e.package == "com.app" for e in events)
    assert events[-1].detail == Status.ALREADY_UP_TO_DATE.value


def test_run_for_device_skips_play_store_entirely_when_skip_update_check(tmp_path):
    """skip_update_check must not just skip *tapping* Update -- it should skip
    launching Play Store and dumping the UI for that package altogether, since
    the whole point is avoiding Play Store UI automation for an already-
    installed package, not just avoiding the tap."""
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(
            adb, "fake-1", ["com.app"], tmp_path, skip_update_check=True
        )

    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert outcome.ok, outcome.error
    assert outcome.status == Status.SKIPPED_UPDATE_CHECK
    assert not any(cmd.startswith("am start") for cmd in adb.shell_log)
    assert "uiautomator dump" not in adb.shell_log


def test_run_for_device_forwards_output_format_to_puller(tmp_path):
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [OPEN]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(
            adb, "fake-1", ["com.app"], tmp_path, output_format=OutputFormat.ZIP
        )

    assert outcomes[0].pulled_files[0].name == "com.app-5.zip"


def test_run_for_device_handles_multiple_packages_on_one_device(tmp_path):
    """Two already-up-to-date packages on the same device: both get kicked off
    and pulled, and both outcomes come back — the core multi-package flow."""
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses["pm path com.other"] = (
        "package:/data/app/com.other-x/base.apk\n"
    )
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [OPEN]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"
    adb.shell_responses["dumpsys package com.other"] = "versionCode=9 versionName=2.0"

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apk_ctor.return_value.version_code = 5
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(
            adb, "fake-1", ["com.app", "com.other"], tmp_path
        )

    assert {o.package for o in outcomes} == {"com.app", "com.other"}
    assert all(o.status == Status.ALREADY_UP_TO_DATE for o in outcomes)
    assert all(o.ok for o in outcomes)


def test_run_for_device_reports_locked_stage_for_a_locked_device():
    """A locked device blocks the whole device, not one package -- LOCKED
    must broadcast to every package on it, like CONNECTING does."""
    adb = _adb_with_device()
    adb.shell_responses["dumpsys window"] = (
        "mShowingDream=true mDreamingLockscreen=true\n"
    )
    events: list[ProgressEvent] = []

    run_for_device(
        adb,
        "fake-1",
        ["com.app", "com.other"],
        None,
        automation_config=AutomationConfig(unlock_timeout=0.01, unlock_poll_interval=0),
        on_progress=events.append,
    )

    locked = [(e.device_id, e.package) for e in events if e.stage == Stage.LOCKED]
    assert set(locked) == {("fake-1", "com.app"), ("fake-1", "com.other")}


def test_run_for_device_distinguishes_downloading_from_updating():
    """DOWNLOADING's detail must say which is actually happening -- an update
    in progress looks nothing like a fresh install to someone watching."""
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [make_dump(("Update", (0, 0, 100, 50)))]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"
    events: list[ProgressEvent] = []

    # The update never actually resolves (static FakeAdb responses) -- bound
    # the completion-pass poll loop so the test doesn't wait out the full
    # default 300s download_timeout; the DOWNLOADING event under test already
    # fired during the kickoff pass, before that loop is ever reached.
    run_for_device(
        adb,
        "fake-1",
        ["com.app"],
        None,
        download_timeout=0.01,
        download_retries=0,
        on_progress=events.append,
    )

    downloading = [e for e in events if e.stage == Stage.DOWNLOADING]
    assert len(downloading) == 1
    assert downloading[0].detail == "update"


def test_run_for_device_restores_stay_on_setting_even_when_automation_fails():
    """stay_on is bumped to 7 before automation starts and must be restored in
    a `finally`, even when automation raises (e.g. a paid-app screen)."""
    adb = _adb_with_device(pkg_pm_path="")  # not installed -> takes the install path
    adb.shell_responses["settings get global stay_on_while_plugged_in"] = "3\n"
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [make_dump(("$4.99", (0, 0, 10, 10)))]

    outcomes, _pending = run_for_device(adb, "fake-1", ["com.app"], None)

    assert outcomes[0].status == Status.ERROR
    assert adb.shell_log.count("settings put global stay_on_while_plugged_in 7") == 1
    assert adb.shell_log[-1] == "settings put global stay_on_while_plugged_in 3"


def test_run_for_device_skips_stay_on_setting_when_keep_screen_on_false(tmp_path):
    adb = _adb_with_device(pkg_pm_path="package:/data/app/com.app-x/base.apk\n")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [OPEN]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(
            adb, "fake-1", ["com.app"], tmp_path, keep_screen_on=False
        )

    assert all(o.ok for o in outcomes)
    assert not any("stay_on_while_plugged_in" in cmd for cmd in adb.shell_log)


INSTALL = make_dump(("Install", (0, 0, 100, 50)))


def test_run_for_device_reports_timeout_when_download_never_finishes():
    """No retries left: a download that never completes must be reported as an
    error instead of polling forever."""
    adb = _adb_with_device(pkg_pm_path="")  # never becomes installed
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [INSTALL]

    outcomes, _pending = run_for_device(
        adb,
        "fake-1",
        ["com.app"],
        None,
        automation_config=AutomationConfig(poll_interval=0),
        download_timeout=1e-9,
        download_retries=0,
    )

    assert len(outcomes) == 1
    assert outcomes[0].status == Status.ERROR
    assert "did not finish" in outcomes[0].error


def test_run_for_device_retries_download_after_timeout_then_succeeds(tmp_path):
    """One retry left: a timed-out download restarts the kickoff flow and
    succeeds once the retry's install actually finishes."""
    adb = _adb_with_device(pkg_pm_path="")
    adb.shell_responses[
        "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
        "cat /sdcard/window_dump.xml"
    ] = [INSTALL, INSTALL]
    # is_installed() is polled 4 times before it finally succeeds: once during
    # the initial kickoff, once right after the initial tap, once in the
    # completion pass (times out), and once right after the retry's tap.
    adb.shell_responses["pm path com.app"] = [
        "",
        "",
        "",
        "",
        "package:/data/app/com.app-x/base.apk\n",
    ]
    adb.shell_responses["dumpsys package com.app"] = "versionCode=5 versionName=1.0"

    with (
        patch("apkpull.puller.ApkFile") as apk_ctor,
        patch("apkpull.bundle.ApksFile") as apks_ctor,
    ):
        apk_ctor.return_value.labels = {"": "App"}
        apk_ctor.return_value.version_code = 5
        apk_ctor.return_value.version_name = "1.0"
        apk_ctor.return_value.min_sdk_version = 21
        apk_ctor.return_value.target_sdk_version = 34
        apks_ctor.return_value.package_name = "com.app"
        apks_ctor.return_value.version_code = 5
        apks_ctor.return_value.as_dict.return_value = {}
        apks_ctor.return_value.splits = []
        configure_apks_create(apks_ctor)

        outcomes = _run_for_device_and_merge(
            adb,
            "fake-1",
            ["com.app"],
            tmp_path,
            automation_config=AutomationConfig(poll_interval=0),
            download_timeout=1e-9,
            download_retries=1,
        )

    assert len(outcomes) == 1
    assert outcomes[0].ok, outcomes[0].error
    assert outcomes[0].status == Status.INSTALLED


# -- run() (multi-device fan-out) ------------------------------------------


def test_run_raises_when_no_devices():
    with patch("apkpull.orchestrator.AdbClient") as adb_ctor:
        adb_ctor.return_value.list_devices.return_value = []
        with pytest.raises(NoDevicesFoundError):
            run("com.app", None, skip_existence_check=True)


def test_run_warns_when_devices_share_a_play_relevant_fingerprint(caplog):
    infos = {
        "dev-1": DeviceInfo(
            device_id="dev-1",
            model="Pixel A",
            abi="arm64-v8a",
            lang="en-US",
            langs=frozenset({"en"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
        "dev-2": DeviceInfo(
            device_id="dev-2",
            model="Pixel B",
            abi="arm64-v8a",
            lang="en-US",
            langs=frozenset({"en"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
        "dev-3": DeviceInfo(
            device_id="dev-3",
            model="Different",
            abi="x86_64",
            lang="en-US",
            langs=frozenset({"en"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
    }

    class _FakeDevice:
        def __init__(self, adb, device_id):
            self.device_id = device_id

        def ensure_connected(self):
            pass

        def info(self):
            return infos[self.device_id]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch("apkpull.orchestrator.Device", _FakeDevice),
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = list(infos)
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Pixel A" in warnings[0]
    assert "Pixel B" in warnings[0]
    assert "Different" not in warnings[0]


def test_run_warns_for_same_density_bucket_despite_different_raw_density(caplog):
    """Two devices reporting different raw dpi (420 vs 439) that round to the
    same Android density bucket (xxhdpi) will get an identical dpi split from
    Play — so this must still warn as a duplicate, not be missed because the
    raw numbers don't match."""
    infos = {
        "dev-1": DeviceInfo(
            device_id="dev-1",
            model="Pixel A",
            abi="arm64-v8a",
            langs=frozenset({"en"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
        "dev-2": DeviceInfo(
            device_id="dev-2",
            model="Pixel B",
            abi="arm64-v8a",
            langs=frozenset({"en"}),
            sdk=34,
            density=439,
            density_bucket="xxhdpi",
        ),
    }

    class _FakeDevice:
        def __init__(self, adb, device_id):
            self.device_id = device_id

        def ensure_connected(self):
            pass

        def info(self):
            return infos[self.device_id]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch("apkpull.orchestrator.Device", _FakeDevice),
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = list(infos)
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "look identical" in warnings[0]
    assert "xxhdpi" in warnings[0]


def test_run_skips_duplicate_check_entirely_when_requested(caplog):
    """skip_duplicate_check must not just filter the warning out — it should skip
    the check's device.info() calls altogether, since they cost a real adb
    round-trip per device."""
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch("apkpull.orchestrator.Device") as device_ctor,
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1", "dev-2"]
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True, skip_duplicate_check=True)

    device_ctor.assert_not_called()
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_run_does_not_warn_for_a_single_device(caplog):
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1"]
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_run_warns_to_consolidate_languages_when_only_langs_differ(caplog):
    """Same hardware (abi/density/sdk), different configured languages: not a
    full duplicate (they'd pull different language splits), but still worth
    flagging — the shared base/abi/density splits get redundantly re-pulled
    when one device configured with every language would do."""
    infos = {
        "dev-1": DeviceInfo(
            device_id="dev-1",
            model="Pixel A",
            abi="arm64-v8a",
            langs=frozenset({"en"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
        "dev-2": DeviceInfo(
            device_id="dev-2",
            model="Pixel B",
            abi="arm64-v8a",
            langs=frozenset({"en", "fr"}),
            sdk=34,
            density=420,
            density_bucket="xxhdpi",
        ),
    }

    class _FakeDevice:
        def __init__(self, adb, device_id):
            self.device_id = device_id

        def ensure_connected(self):
            pass

        def info(self):
            return infos[self.device_id]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch("apkpull.orchestrator.Device", _FakeDevice),
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = list(infos)
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "Pixel A (en)" in warnings[0]
    assert "Pixel B (en,fr)" in warnings[0]
    assert "en,fr" in warnings[0]  # the recommended union to configure on one device
    assert "look identical" not in warnings[0]


def test_run_accepts_a_single_package_string():
    """Library ergonomics: `run("com.app", ...)` still works, normalized to
    a one-element list internally — same call as before this feature."""
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1"]
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    _adb, _device_id, packages_arg, _dest_root = run_for_device_mock.call_args.args
    assert packages_arg == ["com.app"]


def test_run_reraises_keyboard_interrupt_instead_of_swallowing_it(tmp_path):
    """A library caller must be able to catch KeyboardInterrupt itself --
    run() shouldn't silently convert it into a partial RunSummary."""
    fake_pool = MagicMock()
    fake_pool.submit.return_value = MagicMock()

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.ThreadPoolExecutor", return_value=fake_pool),
        patch("apkpull.orchestrator.wait", side_effect=KeyboardInterrupt),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1"]
        with pytest.raises(KeyboardInterrupt):
            run("com.app", tmp_path, skip_existence_check=True)


def test_run_cancels_not_yet_started_devices_on_keyboard_interrupt(tmp_path, caplog):
    """Confirmed hands-on that an untimed wait on thread-pool futures doesn't
    get interrupted by SIGINT at all -- run() must poll with a timeout
    instead, and on interrupt stop launching new device work (cancel()
    only actually takes effect for futures that haven't started) and shut
    the pool down without blocking on already-running ones."""
    futures_not_started = [MagicMock(), MagicMock()]

    fake_pool = MagicMock()
    fake_pool.submit.side_effect = futures_not_started

    # First wait() call is "interrupted" (Ctrl+C); the second simulates the
    # real, expected follow-up once cancel() has synchronously marked both
    # not-yet-started futures CANCELLED — which wait() reports as done.
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.ThreadPoolExecutor", return_value=fake_pool),
        patch(
            "apkpull.orchestrator.wait",
            side_effect=[KeyboardInterrupt, (set(futures_not_started), set())],
        ),
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1", "dev-2"]
        with pytest.raises(KeyboardInterrupt):
            run("com.app", tmp_path, skip_existence_check=True)

    for future in futures_not_started:
        future.cancel.assert_called_once()
    fake_pool.shutdown.assert_called_with(wait=False)
    assert any("Interrupted" in r.message for r in caplog.records)


def test_run_reports_queued_for_every_device_package_pair_upfront(tmp_path):
    """QUEUED must fire for the full (device, package) grid before any device
    thread starts, so a renderer can show the complete worklist immediately
    instead of rows appearing one at a time as threads happen to get
    scheduled."""
    events: list[ProgressEvent] = []

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1", "dev-2"]
        run_for_device_mock.return_value = ([], [])
        run(
            ["com.app", "com.other"],
            tmp_path,
            skip_existence_check=True,
            on_progress=events.append,
        )

    queued = [(e.device_id, e.package) for e in events if e.stage == Stage.QUEUED]
    assert set(queued) == {
        ("dev-1", "com.app"),
        ("dev-1", "com.other"),
        ("dev-2", "com.app"),
        ("dev-2", "com.other"),
    }


def test_run_fans_out_to_every_device_concurrently(tmp_path):
    def make_fake_outcomes(device_id, *_a, **_k):
        from apkpull.models import DeviceInfo, DeviceOutcome

        return [
            DeviceOutcome(
                device=DeviceInfo(device_id=device_id),
                package="com.app",
                status=Status.INSTALLED,
            )
        ]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1", "dev-2", "dev-3"]
        run_for_device_mock.side_effect = lambda adb, device_id, *a, **k: (
            make_fake_outcomes(device_id),
            [],
        )

        summary = run("com.app", tmp_path, skip_existence_check=True)

    assert summary.total == 3
    assert summary.successful == 3
    assert summary.exit_code == 0
    assert {o.device.device_id for o in summary.outcomes} == {"dev-1", "dev-2", "dev-3"}


def test_run_fans_out_multiple_packages_to_every_device(tmp_path):
    def make_fake_outcomes(device_id, packages, *_a, **_k):
        from apkpull.models import DeviceInfo, DeviceOutcome

        return [
            DeviceOutcome(
                device=DeviceInfo(device_id=device_id),
                package=package,
                status=Status.INSTALLED,
            )
            for package in packages
        ]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1", "dev-2"]
        run_for_device_mock.side_effect = lambda adb, device_id, packages, *a, **k: (
            make_fake_outcomes(device_id, packages),
            [],
        )

        summary = run(["com.app", "com.other"], tmp_path, skip_existence_check=True)

    assert summary.total == 4  # 2 devices x 2 packages
    assert {(o.device.device_id, o.package) for o in summary.outcomes} == {
        ("dev-1", "com.app"),
        ("dev-1", "com.other"),
        ("dev-2", "com.app"),
        ("dev-2", "com.other"),
    }


def test_run_respects_explicit_device_ids():
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        from apkpull.models import DeviceInfo, DeviceOutcome

        run_for_device_mock.return_value = (
            [
                DeviceOutcome(
                    device=DeviceInfo(device_id="only-me"),
                    package="com.app",
                    status=Status.INSTALLED,
                )
            ],
            [],
        )
        run("com.app", None, device_ids=["only-me"], skip_existence_check=True)

    adb_ctor.return_value.list_devices.assert_not_called()


# -- cross-device merge (run()'s post-drain _merge_pending_contributions) --------------------


def test_run_merges_distinct_splits_from_two_devices_into_one_bundle(
    tmp_path, force_splits
):
    """The primary end-to-end regression test: two devices with genuinely
    different splits (arm64/en vs x86_64/he) must both end up in the final
    bundle, not just whichever device's thread finished first."""

    # Built lazily, inside the mocked run_for_device -- run() wipes any
    # pre-existing .apkpull-staging at startup (crash-recovery, see
    # test_run_removes_stale_staging_directory_... below), which would
    # otherwise delete these fixtures before the merge phase ever sees them.
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        if device_id == "dev-a":
            c = _make_contribution(
                dest_root,
                "dev-a",
                "com.app",
                100,
                splits=["config.arm64_v8a.apk", "config.en.apk"],
            )
        else:
            c = _make_contribution(
                dest_root,
                "dev-b",
                "com.app",
                100,
                splits=["config.x86_64.apk", "config.he.apk"],
            )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a", "dev-b"]
        run_for_device_mock.side_effect = side_effect
        summary = run(
            "com.app",
            tmp_path,
            skip_existence_check=True,
            skip_duplicate_check=True,
            verify=False,
        )

    target = tmp_path / "com.app-100.apks"
    assert target.exists()
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
    assert {
        "base.apk",
        "config.arm64_v8a.apk",
        "config.en.apk",
        "config.x86_64.apk",
        "config.he.apk",
    } <= names
    assert all(o.pulled_files for o in summary.outcomes)
    assert all(o.destination == target for o in summary.outcomes)


def test_run_forwards_full_manifest_to_build_merged_bundle(tmp_path):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        c = _make_contribution(
            dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
        )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch(
            "apkpull.orchestrator.build_merged_bundle",
            return_value=(tmp_path / "com.app-100.apks", []),
        ) as build_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a"]
        run_for_device_mock.side_effect = side_effect
        run("com.app", tmp_path, skip_existence_check=True, full_manifest=True)

    assert build_mock.call_args.kwargs["full"] is True


def test_run_builds_separate_bundles_for_devices_reporting_different_version_codes(
    tmp_path, caplog, force_splits
):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        if device_id == "dev-a":
            c = _make_contribution(
                dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
            )
        else:
            c = _make_contribution(
                dest_root, "dev-b", "com.app", 200, splits=["config.en.apk"]
            )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a", "dev-b"]
        run_for_device_mock.side_effect = side_effect
        run(
            "com.app",
            tmp_path,
            skip_existence_check=True,
            skip_duplicate_check=True,
            verify=False,
        )

    assert (tmp_path / "com.app-100.apks").exists()
    assert (tmp_path / "com.app-200.apks").exists()
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("different version codes" in w for w in warnings)


def test_run_warns_and_still_merges_when_one_of_three_devices_fails(
    tmp_path, caplog, force_splits
):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        if device_id == "dev-a":
            c = _make_contribution(
                dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
            )
            return [c.outcome], [c]
        if device_id == "dev-b":
            c = _make_contribution(
                dest_root, "dev-b", "com.app", 100, splits=["config.he.apk"]
            )
            return [c.outcome], [c]
        failed_outcome = DeviceOutcome(
            device=DeviceInfo(device_id="dev-c", model="dev-c"),
            package="com.app",
            status=Status.ERROR,
            error="You must be logged in to a Google account.",
        )
        return [failed_outcome], []

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        caplog.at_level("WARNING", logger="apkpull.orchestrator"),
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a", "dev-b", "dev-c"]
        run_for_device_mock.side_effect = side_effect
        summary = run(
            "com.app",
            tmp_path,
            skip_existence_check=True,
            skip_duplicate_check=True,
            verify=False,
        )

    target = tmp_path / "com.app-100.apks"
    assert target.exists()
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
    assert {"config.en.apk", "config.he.apk"} <= names
    warnings = [r.message for r in caplog.records if r.levelname == "WARNING"]
    assert any("missing from" in w and "dev-c" in w for w in warnings)
    assert summary.exit_code == 1  # only dev-c failed


def test_run_marks_all_contributing_devices_error_when_merge_itself_fails(tmp_path):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        if device_id == "dev-a":
            c = _make_contribution(
                dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
            )
        else:
            c = _make_contribution(
                dest_root, "dev-b", "com.app", 100, splits=["config.he.apk"]
            )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch(
            "apkpull.orchestrator.build_merged_bundle",
            side_effect=VerificationError("mismatch"),
        ),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a", "dev-b"]
        run_for_device_mock.side_effect = side_effect
        summary = run(
            "com.app",
            tmp_path,
            skip_existence_check=True,
            skip_duplicate_check=True,
            strict_verify=True,
        )

    assert all(o.status == Status.ERROR for o in summary.outcomes)
    assert all("own install/update succeeded" in o.error for o in summary.outcomes)


def test_run_cleans_up_group_staging_directory_after_a_successful_merge(
    tmp_path, force_splits
):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        c = _make_contribution(
            dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
        )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a"]
        run_for_device_mock.side_effect = side_effect
        run("com.app", tmp_path, skip_existence_check=True, verify=False)

    assert not (tmp_path / ".apkpull-staging" / "com.app-100").exists()


def test_run_cleans_up_group_staging_directory_after_a_failed_merge(tmp_path):
    def side_effect(adb, device_id, packages, dest_root, **kwargs):
        c = _make_contribution(
            dest_root, "dev-a", "com.app", 100, splits=["config.en.apk"]
        )
        return [c.outcome], [c]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch(
            "apkpull.orchestrator.build_merged_bundle", side_effect=PullError("boom")
        ),
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-a"]
        run_for_device_mock.side_effect = side_effect
        run("com.app", tmp_path, skip_existence_check=True)

    assert not (tmp_path / ".apkpull-staging" / "com.app-100").exists()


def test_run_removes_stale_staging_directory_left_by_a_previous_crashed_run_at_startup(
    tmp_path,
):
    stale = tmp_path / ".apkpull-staging" / "com.old-1" / "dev-x"
    stale.mkdir(parents=True)
    (stale / "base.apk").write_bytes(b"leftover")

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1"]
        run_for_device_mock.return_value = ([], [])
        run("com.app", tmp_path, skip_existence_check=True)

    assert not (tmp_path / ".apkpull-staging").exists()


# -- device-info reuse / unexpected-exception handling ----------------------


def test_warn_about_duplicate_devices_returns_resolved_infos_by_device_id():
    infos = {
        "dev-1": DeviceInfo(device_id="dev-1", model="Pixel A"),
        "dev-2": DeviceInfo(device_id="dev-2", model="Pixel B"),
    }

    class _FakeDevice:
        def __init__(self, adb, device_id):
            self.device_id = device_id

        def ensure_connected(self):
            pass

        def info(self):
            return infos[self.device_id]

    with patch("apkpull.orchestrator.Device", _FakeDevice):
        result = _warn_about_duplicate_devices(MagicMock(), ["dev-1", "dev-2"])

    assert result == infos


def test_run_passes_resolved_device_info_to_run_for_device_avoiding_a_second_fetch():
    """The duplicate-device check (run before the thread pool starts) already
    resolved each device's info -- run() should hand that straight to
    run_for_device instead of letting it redundantly re-fetch the same
    getprop/locale round trip a second time for the same device."""
    infos = {
        "dev-1": DeviceInfo(device_id="dev-1", model="Pixel A", abi="arm64-v8a"),
        "dev-2": DeviceInfo(device_id="dev-2", model="Pixel B", abi="x86_64"),
    }

    class _FakeDevice:
        def __init__(self, adb, device_id):
            self.device_id = device_id

        def ensure_connected(self):
            pass

        def info(self):
            return infos[self.device_id]

    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
        patch("apkpull.orchestrator.Device", _FakeDevice),
    ):
        adb_ctor.return_value.list_devices.return_value = list(infos)
        run_for_device_mock.return_value = ([], [])
        run("com.app", None, skip_existence_check=True)

    seen = {
        call.args[1]: call.kwargs["device_info"]
        for call in run_for_device_mock.call_args_list
    }
    assert seen == infos


def test_run_propagates_unexpected_exception_from_run_for_device():
    """A genuine bug in run_for_device (not a per-device DeviceError, which it
    already turns into a normal error outcome rather than raising) must still
    surface loudly instead of being silently swallowed."""
    with (
        patch("apkpull.orchestrator.AdbClient") as adb_ctor,
        patch("apkpull.orchestrator.run_for_device") as run_for_device_mock,
    ):
        adb_ctor.return_value.list_devices.return_value = ["dev-1"]
        run_for_device_mock.side_effect = RuntimeError("unexpected bug")
        with pytest.raises(RuntimeError, match="unexpected bug"):
            run("com.app", None, skip_existence_check=True, skip_duplicate_check=True)


def test_drain_batch_collects_all_successes_even_when_one_future_raises():
    """`done` from concurrent.futures.wait() is an unordered set -- an
    unexpected exception from one device's future must not prevent a
    sibling's already-computed result, sitting in the same batch, from being
    collected."""
    outcome = DeviceOutcome(
        device=DeviceInfo(device_id="dev-1"),
        package="com.app",
        status=Status.INSTALLED,
    )
    good: Future = Future()
    good.set_result(([outcome], []))
    bad: Future = Future()
    bad.set_exception(RuntimeError("boom"))

    outcomes: list = []
    contributions: list = []
    exceptions = _drain_batch({good, bad}, outcomes, contributions)

    assert outcomes == [outcome]
    assert len(exceptions) == 1
    assert isinstance(exceptions[0], RuntimeError)


def test_drain_batch_skips_cancelled_futures_without_reporting_them_as_errors():
    cancelled: Future = Future()
    cancelled.cancel()

    outcomes: list = []
    contributions: list = []
    exceptions = _drain_batch({cancelled}, outcomes, contributions)

    assert outcomes == []
    assert exceptions == []
