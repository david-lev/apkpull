"""Shared test helpers: build fake uiautomator dumps and a fake adb backend."""

from __future__ import annotations

import zipfile
from pathlib import Path

from apkpull.bundle import BASE_NAME

_DATA_DIR = Path(__file__).parent / "data" / "apk"
POLITEDROID_BYTES = (_DATA_DIR / "politedroid.apk").read_bytes()
TEST_DEBUG_BYTES = (_DATA_DIR / "test-debug.apk").read_bytes()


def make_dump(*texts_and_bounds: tuple[str, tuple[int, int, int, int]]) -> str:
    """Build a minimal uiautomator XML dump containing the given text nodes.

    >>> make_dump(("Install", (0, 0, 100, 50)))
    """
    nodes = "".join(
        f'<node text="{text}" bounds="[{x1},{y1}][{x2},{y2}]"/>'
        for text, (x1, y1, x2, y2) in texts_and_bounds
    )
    return f'<?xml version="1.0"?><hierarchy rotation="0">{nodes}</hierarchy>'


def make_icon_dump(
    content_desc: str,
    text: str,
    *,
    icon_bounds: tuple[int, int, int, int] = (36, 1156, 84, 1204),
    text_bounds: tuple[int, int, int, int] = (132, 1150, 881, 1210),
) -> str:
    """Build a dump pairing a text-less icon node (identified only by
    ``content-desc``) with a text node on the same row — mirrors Play Store's
    real warning-triangle-plus-message layout (confirmed hands-on: an
    ``ImageView`` with ``content-desc="Warning"`` immediately followed by a
    ``TextView`` with the error message, same row of bounds).
    """
    x1, y1, x2, y2 = icon_bounds
    tx1, ty1, tx2, ty2 = text_bounds
    return (
        '<?xml version="1.0"?><hierarchy rotation="0">'
        f'<node text="" content-desc="{content_desc}" bounds="[{x1},{y1}][{x2},{y2}]"/>'
        f'<node text="{text}" bounds="[{tx1},{ty1}][{tx2},{ty2}]"/>'
        "</hierarchy>"
    )


BUTTON_BOUNDS = (100, 200, 300, 260)


class FakeAdb:
    """Duck-types :class:`apkpull.adb.AdbClient` without touching a real subprocess.

    ``shell_responses`` maps an exact shell command string to either:
      * a plain string (returned every time),
      * a list of strings (popped in order, last element repeats once exhausted),
      * a zero-arg callable (invoked for its return value).
    """

    def __init__(self) -> None:
        self.adb_path = "fake-adb"
        self.shell_responses: dict[str, object] = {}
        self.shell_log: list[str] = []
        self.pulled: list[tuple[str, Path]] = []
        self.connected = True
        self.state = "device"
        # ApksFile.create() (invoked by build_apks_bundle()) re-parses every pulled
        # apk for real, so base/split pulls need to land real, valid apk bytes on
        # disk -- OBB pulls don't, since nothing ever parses those.
        self.base_apk_bytes = POLITEDROID_BYTES
        self.split_apk_bytes = TEST_DEBUG_BYTES
        self.obb_bytes = b"FAKE-OBB-CONTENT"

    # -- discovery --------------------------------------------------------

    def list_devices(self) -> list[str]:
        return ["fake-1"] if self.connected else []

    def get_state(self, device_id: str) -> str:
        return self.state if self.connected else "offline"

    def is_connected(self, device_id: str) -> bool:
        return self.connected and self.state == "device"

    # -- shell --------------------------------------------------------------

    def shell(
        self, device_id: str, command: str, *, timeout: float = 30.0, check: bool = True
    ) -> str:
        self.shell_log.append(command)
        response = self.shell_responses.get(command, "")
        if isinstance(response, list):
            value = response.pop(0) if len(response) > 1 else response[0]
        elif callable(response):
            value = response()
        else:
            value = response
        return value

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
        if local_path.suffix == ".obb":
            local_path.write_bytes(self.obb_bytes)
        elif local_path.name == BASE_NAME:
            local_path.write_bytes(self.base_apk_bytes)
        else:
            local_path.write_bytes(self.split_apk_bytes)
        self.pulled.append((remote_path, local_path))

    def exec_out_to_file(
        self, device_id: str, command: str, local_path: Path, *, timeout: float = 30.0
    ) -> None:
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(b"FAKE-PNG")


def configure_apks_create(apks_ctor) -> None:
    """For tests that fully mock ``apkpull.bundle.ApksFile`` (both the
    ``ApksFile(path)`` constructor ``verify_bundle`` uses and the
    ``ApksFile.create()`` classmethod ``build_apks_bundle`` uses): give
    ``.create()`` a side effect that actually writes a (trivially valid,
    empty) zip to the requested output path, mirroring ``apks_ctor``'s
    return value. Without this, ``build_apks_bundle``'s tmp-file rename
    into place raises ``FileNotFoundError`` since a bare ``MagicMock``
    ``.create()`` call has no on-disk effect at all.
    """

    def _create(apks, output_path, **kwargs):
        with zipfile.ZipFile(output_path, "w"):
            pass
        return apks_ctor.return_value

    apks_ctor.create.side_effect = _create
