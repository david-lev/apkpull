"""Integration tests against a real adb-connected device/emulator.

Deliberately safe: everything here targets ``com.android.vending`` (the
Play Store app itself), which is always already installed on any device
that can run apkpull, so these tests never install/update/uninstall
anything real — they only exercise device discovery, the automation
"already up to date" branch, pulling, and apkfile-based verification
against genuine adb/apk data.

Skipped automatically when no adb binary or no connected device is found,
so the rest of the suite stays runnable without hardware/an emulator.
"""

from __future__ import annotations

import shutil

import pytest

from apkpull.adb import AdbClient
from apkpull.device import Device
from apkpull.orchestrator import run

GOOGLE_PLAY_PACKAGE = "com.android.vending"


def _connected_device_id() -> str | None:
    if not shutil.which("adb"):
        return None
    devices = AdbClient().list_devices()
    return devices[0] if devices else None


DEVICE_ID = _connected_device_id()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        DEVICE_ID is None, reason="no adb-connected device/emulator available"
    ),
]


@pytest.fixture(scope="module")
def adb() -> AdbClient:
    return AdbClient()


@pytest.fixture(scope="module")
def device(adb: AdbClient) -> Device:
    return Device(adb, DEVICE_ID)


def test_list_devices_sees_the_target_device(adb: AdbClient):
    assert DEVICE_ID in adb.list_devices()


def test_device_info_reports_real_properties(device: Device):
    info = device.info(refresh=True)
    assert info.model != "unknown"
    assert info.abi != "unknown"
    assert info.lang != "unknown"


def test_google_play_is_installed_and_enabled(device: Device):
    assert device.is_installed(GOOGLE_PLAY_PACKAGE)
    assert not device.is_disabled(GOOGLE_PLAY_PACKAGE)


def test_dump_ui_returns_non_empty_xml(device: Device):
    dump = device.dump_ui()
    assert "<hierarchy" in dump


@pytest.mark.timeout(90)
def test_full_pull_and_verify_pipeline_against_real_device(tmp_path):
    """End-to-end: automation + pull + apkfile verification, all against real adb output."""
    summary = run(
        GOOGLE_PLAY_PACKAGE,
        tmp_path,
        device_ids=[DEVICE_ID],
        skip_existence_check=True,  # com.android.vending has no public Play Store listing
        verify=True,
    )

    assert summary.total == 1
    outcome = summary.outcomes[0]
    assert outcome.ok, outcome.error
    assert outcome.version_code is not None
    assert outcome.pulled_files, "expected at least the .apks bundle to be pulled"

    bundle = next(f for f in outcome.pulled_files if f.kind.value == "bundle")
    assert bundle.local_path.is_file()
    assert bundle.local_path.stat().st_size > 0
    assert bundle.local_path.suffix == ".apks"
    assert bundle.verified is True

    manifest_path = bundle.local_path.with_suffix(".manifest.json")
    assert manifest_path.is_file()
