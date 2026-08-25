"""Plain data types shared across apkpull.

Kept dependency-free (stdlib only) so they're cheap to import from tests,
and trivial to serialize with :func:`dataclasses.asdict` for ``--json`` output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class FileKind(str, Enum):
    BUNDLE = "bundle"
    """The base apk + splits, as one artifact — a zip (.apks/.zip) or an extracted folder."""
    OBB = "obb"


class OutputFormat(str, Enum):
    APKS = "apks"
    """SAI/bundletool ``.apks``: base apk + splits + ``meta.sai_v2.json``, zipped."""
    ZIP = "zip"
    """Same contents as APKS, just named ``.zip`` for tools that expect a plain zip."""
    FOLDER = "folder"
    """Extracted: base apk + splits as loose files in a ``<package>-<version_code>/`` directory."""


class Status(str, Enum):
    INSTALLED = "installed"
    UPDATED = "updated"
    ALREADY_UP_TO_DATE = "already_up_to_date"
    SKIPPED_UPDATE_CHECK = "skipped_update_check"
    """Package was already installed and ``skip_update_check`` was set, so apkpull
    pulled whatever's currently on the device without ever asking Play Store
    whether a newer version exists — unlike ALREADY_UP_TO_DATE, this is not a
    claim that the installed version actually is current."""
    UNSUPPORTED_LOCALE = "unsupported_locale"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class PulledFile:
    """A single apk/obb file that was (or would have been) copied to the host."""

    kind: FileKind
    name: str
    local_path: Path
    size_bytes: int
    already_existed: bool = False
    verified: bool | None = None
    """``None`` = not checked, ``True``/``False`` = apkfile verification result."""

    def as_dict(self) -> dict:
        d = {
            "kind": self.kind.value,
            "name": self.name,
            "local_path": str(self.local_path),
            "size_bytes": self.size_bytes,
            "already_existed": self.already_existed,
            "verified": self.verified,
        }
        return d


@dataclass(slots=True)
class DeviceInfo:
    """Static-ish facts about a device, cached for the lifetime of a run."""

    device_id: str
    model: str = "unknown"
    abi: str = "unknown"
    lang: str = "unknown"
    """Primary locale (e.g. ``en-US``) — which button-text table drives automation."""
    langs: frozenset[str] = frozenset()
    """Base subtag of every locale configured on the device (e.g. ``{"en", "he"}``) — Play
    fetches a language split per *installed* language, not just the primary."""
    sdk: int | None = None
    density: int | None = None
    """Raw screen density (dpi) as reported by the device — not what decides which
    dpi split Play serves; see ``density_bucket``."""
    density_bucket: str | None = None
    """Nearest standard Android density bucket for ``density`` (e.g. ``"xxhdpi"``)
    — this, not the raw value, is what actually determines which dpi split Google
    Play serves: two devices with different ``density`` but the same bucket get
    identical dpi splits. A plain ``str`` (not apkfile's ``DensityBucket`` enum) so
    this module stays dependency-free — see module docstring."""

    @property
    def fingerprint(self) -> tuple[str, str | None, frozenset[str], int | None]:
        """The subset of properties Google Play uses to pick which apk/splits
        to serve. Two devices sharing a fingerprint would get identical
        downloads."""
        return (self.abi, self.density_bucket, self.langs, self.sdk)

    @property
    def label(self) -> str:
        return self.model if self.model != "unknown" else self.device_id


@dataclass(slots=True)
class DeviceOutcome:
    """Result of running apkpull for one package on one device."""

    device: DeviceInfo
    package: str
    status: Status
    pulled_files: list[PulledFile] = field(default_factory=list)
    version_code: int | None = None
    version_name: str | None = None
    destination: Path | None = None
    error: str | None = None
    duration_s: float = 0.0
    uninstalled: bool = False
    """Note ``uninstalled=True`` together with ``status=Status.ERROR`` is a
    valid combination: uninstalling happens right after this device's own
    raw pull finishes, before its splits are merged with any other device's
    into a shared bundle — a later failure in that merge (or a sibling
    device's contribution) can still flip ``status`` to ``ERROR`` even
    though this device's own install/update and uninstall already
    succeeded."""

    @property
    def ok(self) -> bool:
        return self.status in (
            Status.INSTALLED,
            Status.UPDATED,
            Status.ALREADY_UP_TO_DATE,
            Status.SKIPPED_UPDATE_CHECK,
        )

    def as_dict(self) -> dict:
        return {
            "device_id": self.device.device_id,
            "model": self.device.model,
            "abi": self.device.abi,
            "lang": self.device.lang,
            "langs": sorted(self.device.langs),
            "sdk": self.device.sdk,
            "density": self.device.density,
            "density_bucket": self.device.density_bucket,
            "package": self.package,
            "status": self.status.value,
            "ok": self.ok,
            "version_code": self.version_code,
            "version_name": self.version_name,
            "destination": str(self.destination) if self.destination else None,
            "pulled_files": [f.as_dict() for f in self.pulled_files],
            "error": self.error,
            "duration_s": round(self.duration_s, 2),
            "uninstalled": self.uninstalled,
        }


@dataclass(slots=True)
class RunSummary:
    """Aggregate result across every device targeted by a single run."""

    outcomes: list[DeviceOutcome] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.outcomes)

    @property
    def successful(self) -> int:
        return sum(1 for o in self.outcomes if o.ok)

    @property
    def exit_code(self) -> int:
        """0 if every device succeeded, otherwise the number of failures (capped at 9)."""
        failures = self.total - self.successful
        if self.total == 0:
            return 50
        return min(failures, 9)

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "successful": self.successful,
            "exit_code": self.exit_code,
            "devices": [o.as_dict() for o in self.outcomes],
        }
