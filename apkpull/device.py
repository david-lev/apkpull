"""High level, per-device operations built on top of :class:`apkpull.adb.AdbClient`.

Everything here still deals only in adb/shell primitives — no Play Store /
UI-automation knowledge lives in this module (that's :mod:`apkpull.automation`).
"""

from __future__ import annotations

import logging
import re

from apkfile import DensityBucket

from .adb import AdbClient
from .exceptions import DeviceDisconnectedError
from .models import DeviceInfo

logger = logging.getLogger("apkpull.device")

_GETPROP_RE = re.compile(r"\[([\w.\-]+)\]:\s*\[(.*?)\]")
_VERSION_CODE_RE = re.compile(r"versionCode=(\d+)")
_VERSION_NAME_RE = re.compile(r"versionName=([^\s]+)")
_APP_LOCALES_RE = re.compile(r"are \[(.*?)\]")

MIN_SDK_FOR_APP_LOCALES = 33
"""Per-app locale overrides (``cmd locale ...``) landed in Android 13."""

# The real, physical-density buckets Android resource resolution (and Google
# Play's dpi-split selection) picks among — excludes DensityBucket's
# resource-qualifier-only sentinels (DEFAULT/ANY/NONE), which no device's
# actual screen density ever reports as.
_REAL_DENSITY_BUCKETS = (
    DensityBucket.LDPI,
    DensityBucket.MDPI,
    DensityBucket.TVDPI,
    DensityBucket.HDPI,
    DensityBucket.XHDPI,
    DensityBucket.XXHDPI,
    DensityBucket.XXXHDPI,
)


def _nearest_density_bucket(dpi: int) -> str:
    """Google Play always serves one of a fixed set of density buckets, never a
    device's exact raw dpi — this finds the nearest one by absolute distance.
    Approximate: Android's real resource-matching algorithm has some
    asymmetric preference for scaling a higher-density asset down over a
    lower-density one up, which a plain nearest-neighbor doesn't model — but
    it's right for the vast majority of real device densities, which don't
    sit exactly on a bucket boundary.
    """
    return min(_REAL_DENSITY_BUCKETS, key=lambda b: abs(b.dpi - dpi)).value


_MIN_SDK_RE = re.compile(r"minSdk=(\d+)")


class Device:
    """A single adb-connected device/emulator, identified by ``device_id``."""

    def __init__(self, adb: AdbClient, device_id: str) -> None:
        self.adb = adb
        self.device_id = device_id
        self._info: DeviceInfo | None = None

    def __repr__(self) -> str:  # pragma: no cover - cosmetic
        return f"Device({self.device_id!r})"

    # -- connection state -------------------------------------------------

    def is_connected(self) -> bool:
        return self.adb.is_connected(self.device_id)

    def ensure_connected(self) -> None:
        if not self.is_connected():
            state = self.adb.get_state(self.device_id)
            raise DeviceDisconnectedError(f"Device {self.device_id} is {state}!")

    # -- identity -----------------------------------------------------------

    def seed_info(self, info: DeviceInfo) -> None:
        """Pre-populate the cached :meth:`info` result from an already-resolved
        lookup done elsewhere (e.g. the duplicate-device check in
        ``orchestrator.py``), so a later :meth:`info` call on this instance
        returns it directly instead of redundantly re-issuing the same
        ``getprop``/locale round trip for the same device within one run."""
        self._info = info

    def info(self, *, refresh: bool = False) -> DeviceInfo:
        if self._info is not None and not refresh:
            return self._info
        raw = self.adb.shell(self.device_id, "getprop")
        props = dict(_GETPROP_RE.findall(raw))
        lang, langs = self._resolve_locales(props)
        density = self._to_int(
            props.get("ro.sf.lcd_density") or props.get("qemu.sf.lcd_density")
        )
        self._info = DeviceInfo(
            device_id=self.device_id,
            model=props.get("ro.product.model", "unknown"),
            abi=props.get("ro.product.cpu.abi", "unknown"),
            lang=lang,
            langs=langs,
            sdk=self._to_int(props.get("ro.build.version.sdk")),
            density=density,
            density_bucket=_nearest_density_bucket(density)
            if density is not None
            else None,
        )
        return self._info

    @staticmethod
    def _to_int(value: str | None) -> int | None:
        return int(value) if value and value.isdigit() else None

    def _resolve_locales(self, props: dict[str, str]) -> tuple[str, frozenset[str]]:
        """Same fallback chain as apkfile's ``install._device_lang_codes``: a fresh/headless
        emulator that never went through Setup Wizard can leave ``persist.sys.locale(s)``
        completely unset (confirmed hands-on by apkfile), even though a real, user-configured
        device always has one of these populated. Falls through, in order: the full priority
        locale list, the single primary locale, the live settings-provider value, and finally
        the read-only build-time default — which is always present.

        Returns ``(lang, langs)``: ``lang`` is the primary locale in its full
        ``persist.sys.locale``-style form (e.g. ``en-US``, used to pick which Play Store
        button-text table to drive); ``langs`` is the base-subtag set across *every* configured
        locale (e.g. ``{"en", "he"}`` for ``en-US,he-IL``) — Play Store fetches a language split
        per *installed* language, not just the primary one, so this is what actually predicts
        which splits a pull will contain.
        """
        for key in ("persist.sys.locales", "persist.sys.locale"):
            raw = props.get(key, "").strip()
            if raw and raw != "null":
                return self._split_locales(raw)
        raw = self.adb.shell(
            self.device_id, "settings get system system_locales", check=False
        ).strip()
        if raw and raw != "null":
            return self._split_locales(raw)
        raw = props.get("ro.product.locale", "").strip()
        return self._split_locales(raw) if raw else ("unknown", frozenset())

    @staticmethod
    def _split_locales(raw: str) -> tuple[str, frozenset[str]]:
        tags = [tag for tag in raw.split(",") if tag]
        primary = tags[0] if tags else "unknown"
        return primary, frozenset(tag.split("-")[0] for tag in tags)

    @property
    def label(self) -> str:
        return self.info().label

    # -- package queries -----------------------------------------------------

    def is_installed(self, package: str) -> bool:
        result = self.adb.shell(self.device_id, f"pm path {package}", check=False)
        return bool(result.strip())

    def is_disabled(self, package: str) -> bool:
        result = self.adb.shell(self.device_id, "pm list packages -d", check=False)
        return any(line.strip() == f"package:{package}" for line in result.splitlines())

    def apk_paths(self, package: str) -> list[str]:
        result = self.adb.shell(self.device_id, f"pm path {package}", check=False)
        return [
            line.removeprefix("package:").strip()
            for line in result.splitlines()
            if line.strip()
        ]

    def dumpsys_package(self, package: str) -> str:
        return self.adb.shell(self.device_id, f"dumpsys package {package}", check=False)

    def version_code(self, package: str) -> int | None:
        match = _VERSION_CODE_RE.search(self.dumpsys_package(package))
        return int(match.group(1)) if match else None

    def version_name(self, package: str) -> str | None:
        match = _VERSION_NAME_RE.search(self.dumpsys_package(package))
        return match.group(1) if match else None

    def min_sdk(self, package: str) -> int | None:
        match = _MIN_SDK_RE.search(self.dumpsys_package(package))
        return int(match.group(1)) if match else None

    def file_exists(self, remote_path: str) -> bool:
        result = self.adb.shell(
            self.device_id, f"test -f '{remote_path}' && echo yes", check=False
        )
        return result.strip() == "yes"

    def remote_size_human(self, remote_path: str) -> str:
        result = self.adb.shell(self.device_id, f"du -sh '{remote_path}'", check=False)
        return result.split()[0].strip() if result.strip() else "?"

    # -- screen / input -----------------------------------------------------

    def is_unlocked(self) -> bool:
        result = self.adb.shell(self.device_id, "dumpsys window", check=False)
        return "mShowingDream=false mDreamingLockscreen=false" in result

    def focused_window(self) -> str:
        """The currently focused window, e.g. ``mCurrentFocus=Window{... com.android.vending/...}``.

        Deliberately reads ``dumpsys window``'s ``mCurrentFocus`` rather than
        ``dumpsys activity activities``' resumed-activity field: the latter's
        field name isn't stable across Android versions — confirmed hands-on
        that ``mResumedActivity`` is simply absent from that dump on Android
        14 (API 34), replaced by ``topResumedActivity``/``ResumedActivity:``.
        Parsing for whichever field name happened to exist previously made
        this silently and permanently return "not on Google Play", causing
        an unconditional relaunch on every single poll — see the automation
        log spam this produced (``Left Google Play, relaunching...`` every
        iteration) even while genuinely sitting on the right page.
        ``mCurrentFocus`` has been a stable, single-line answer across
        versions in comparison.
        """
        result = self.adb.shell(self.device_id, "dumpsys window", check=False)
        for line in result.splitlines():
            if "mCurrentFocus" in line:
                return line.strip()
        return ""

    def is_foreground_package(self, package: str) -> bool:
        return package in self.focused_window()

    def tap(self, x: int, y: int) -> None:
        self.adb.shell(self.device_id, f"input tap {x} {y}", check=False)

    def launch_url(self, url: str, package: str) -> None:
        self.adb.shell(
            self.device_id,
            f"am start -a android.intent.action.VIEW -d '{url}' -p {package}",
            check=False,
        )

    def dump_ui(self) -> str:
        # One combined shell round-trip instead of three separate adb subprocess
        # calls (rm, dump, cat) -- this is the single most-executed adb sequence
        # in the whole tool (runs on every automation poll, for every device),
        # so cutting adb subprocess overhead here matters a lot in aggregate.
        # `uiautomator dump`'s own stdout ("UI hierarchy dumped to: ...") is
        # redirected away so only `cat`'s output comes back.
        return self.adb.shell(
            self.device_id,
            "rm -f /sdcard/window_dump.xml; uiautomator dump >/dev/null 2>&1; "
            "cat /sdcard/window_dump.xml",
            check=False,
            timeout=15,
        )

    # -- app-scoped locale (Android 13+ / API 33+) ---------------------------

    def get_app_locale(self, package: str) -> str:
        """``package``'s current per-app locale override, e.g. ``"fr"`` — empty
        string means it follows the system locale. Confirmed hands-on: ``cmd
        locale get-app-locales`` replies ``"Locales for <pkg> for user 0 are
        [<tag>]"`` (``[]`` when unset), on every Android version that has the
        command at all -- callers still need their own SDK gate before relying
        on this being meaningful (see ``MIN_SDK_FOR_APP_LOCALES``)."""
        result = self.adb.shell(
            self.device_id,
            f"cmd locale get-app-locales {package} --user 0",
            check=False,
        )
        match = _APP_LOCALES_RE.search(result)
        return match.group(1) if match else ""

    def set_app_locale(self, package: str, locale: str) -> None:
        """Override ``package``'s locale, or clear the override back to the
        system default when ``locale`` is ``""``. Only takes effect once the
        app's process restarts -- ``force_stop`` it afterward."""
        self.adb.shell(
            self.device_id,
            f"cmd locale set-app-locales {package} --user 0 --locales {locale}",
            check=False,
        )

    def force_stop(self, package: str) -> None:
        self.adb.shell(self.device_id, f"am force-stop {package}", check=False)

    # -- settings -------------------------------------------------------------

    def get_setting(self, namespace: str, key: str) -> str:
        return self.adb.shell(
            self.device_id, f"settings get {namespace} {key}", check=False
        ).strip()

    def put_setting(self, namespace: str, key: str, value: str) -> None:
        self.adb.shell(
            self.device_id, f"settings put {namespace} {key} {value}", check=False
        )
