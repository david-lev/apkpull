import subprocess
from unittest.mock import patch

import pytest

from apkpull.adb import AdbClient
from apkpull.exceptions import AdbNotFoundError, DeviceDisconnectedError


def _completed(stdout="", stderr="", returncode=0):
    return subprocess.CompletedProcess(
        args=[], returncode=returncode, stdout=stdout, stderr=stderr
    )


@pytest.fixture
def adb():
    with patch("shutil.which", return_value="/usr/local/bin/adb"):
        return AdbClient()


def test_raises_when_adb_not_on_path():
    with patch("shutil.which", return_value=None), pytest.raises(AdbNotFoundError):
        AdbClient()


def test_explicit_adb_path_skips_which():
    client = AdbClient(adb_path="/custom/adb")
    assert client.adb_path == "/custom/adb"


def test_list_devices_filters_to_ready_state(adb):
    stdout = "List of devices attached\nemulator-5554\tdevice\n192.168.1.5:5555\tunauthorized\n\n"
    with patch("subprocess.run", return_value=_completed(stdout=stdout)) as run:
        devices = adb.list_devices()
    assert devices == ["emulator-5554"]
    assert run.call_args.args[0][0] == "/usr/local/bin/adb"


def test_get_state_returns_stripped_stdout(adb):
    with patch("subprocess.run", return_value=_completed(stdout="device\n")):
        assert adb.get_state("emulator-5554") == "device"


def test_get_state_cleans_up_error_message(adb):
    with patch(
        "subprocess.run",
        return_value=_completed(stderr="error: device offline\n", returncode=1),
    ):
        assert adb.get_state("emulator-5554") == "offline"


def test_is_connected_true_only_for_device_state(adb):
    with patch("subprocess.run", return_value=_completed(stdout="device\n")):
        assert adb.is_connected("emulator-5554") is True
    with patch("subprocess.run", return_value=_completed(stdout="offline\n")):
        assert adb.is_connected("emulator-5554") is False


def test_shell_returns_stdout(adb):
    with patch("subprocess.run", return_value=_completed(stdout="hello\n")):
        assert adb.shell("emulator-5554", "echo hello") == "hello\n"


def test_shell_check_raises_on_failure(adb):
    with (
        patch("subprocess.run", return_value=_completed(stderr="boom", returncode=1)),
        pytest.raises(DeviceDisconnectedError),
    ):
        adb.shell("emulator-5554", "false")


def test_shell_check_false_swallows_failure(adb):
    with patch("subprocess.run", return_value=_completed(stderr="boom", returncode=1)):
        assert adb.shell("emulator-5554", "false", check=False) == ""


def test_shell_timeout_raises_disconnected(adb):
    with (
        patch(
            "subprocess.run",
            side_effect=subprocess.TimeoutExpired(cmd="adb", timeout=1),
        ),
        pytest.raises(DeviceDisconnectedError),
    ):
        adb.shell("emulator-5554", "sleep 999")


def test_pull_creates_parent_dir_and_raises_on_failure(adb, tmp_path):
    dest = tmp_path / "nested" / "out.apk"
    with (
        patch(
            "subprocess.run",
            return_value=_completed(returncode=1, stderr="no such file"),
        ),
        pytest.raises(DeviceDisconnectedError),
    ):
        adb.pull("emulator-5554", "/sdcard/x.apk", dest)
    assert dest.parent.is_dir()
