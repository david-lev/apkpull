"""Pulls a package's apks off a device, and merges every targeted device's
contribution into one bundle.

Output lands flat in one destination directory, one artifact per (package,
version), named by :class:`~apkpull.models.OutputFormat`:

    <dest>/<package>-<version_code>.apks               # OutputFormat.APKS (default)
    <dest>/<package>-<version_code>.zip                 # OutputFormat.ZIP
    <dest>/<package>-<version_code>/                    # OutputFormat.FOLDER (extracted)
    <dest>/<package>-<version_code>.manifest.json       # apks/zip: sidecar; folder: inside it
    <dest>/main.<version_code>.<package>.obb            # only if the app ships one (rare)

Every format is built the same way under the hood: base + splits are always
zipped into a bundle first (see :mod:`apkpull.bundle`) and verified with
apkfile *before* the requested format is materialized — that's what lets
FOLDER get the exact same verification/manifest coverage as APKS/ZIP despite
not being a zip on disk itself.

Two-phase pipeline, split across this module's two entry points:

- :meth:`Puller.pull_raw` — per-device I/O. Pulls base.apk + splits (+ any
  OBB) off ONE device into a persistent staging directory and returns a
  :class:`RawPull` describing what landed there, without building anything.
  Runs inside that device's own thread in :mod:`apkpull.orchestrator`.
- :func:`build_merged_bundle` — run-wide, single-threaded, no adb. Takes
  every device's :class:`RawPull` for one ``(package, version_code)`` once
  they've *all* finished (success or failure), unions their distinct splits
  (and OBBs) by filename, and builds ONE bundle from the result — so a
  package pulled from three differently-configured devices ends up as a
  single universal artifact, not three redundant/incomplete ones. Called
  from :func:`apkpull.orchestrator._merge_pending_contributions` once every
  targeted device has resolved.

An artifact that already exists locally (from an earlier, separate apkpull
invocation — never from a sibling device in the *same* run, since merging
only ever happens after every device has already finished) is never
rebuilt, letting a rerun skip straight past devices whose work is already
done without touching them at all.
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from apkfile import ApkFile, InvalidApkError

from .bundle import BASE_NAME, build_apks_bundle, json_default, verify_bundle
from .device import Device
from .exceptions import PullError
from .models import FileKind, OutputFormat, PulledFile
from .progress import PackageReporter, Stage

logger = logging.getLogger("apkpull.puller")

_STAGING_DIRNAME = ".apkpull-staging"


def size_of(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


def target_path(
    dest_root: Path, package: str, version_code: int, output_format: OutputFormat
) -> Path:
    """Where a (package, version_code) bundle lands — a pure function of its
    inputs, no device or filesystem I/O, so it's knowable before anything is
    pulled."""
    stem = f"{package}-{version_code}"
    suffix = {
        OutputFormat.APKS: ".apks",
        OutputFormat.ZIP: ".zip",
        OutputFormat.FOLDER: "",
    }[output_format]
    return dest_root / f"{stem}{suffix}"


def materialize(
    zip_path: Path, target: Path, output_format: OutputFormat, manifest: dict | None
) -> None:
    """Move/extract a just-built bundle zip into its final ``target``."""
    target.parent.mkdir(parents=True, exist_ok=True)
    if output_format == OutputFormat.FOLDER:
        # Extract to a sibling temp dir first, then atomically rename into place — same
        # crash-safety reasoning as build_apks_bundle's own .tmp-then-replace, so a partial
        # extraction (e.g. interrupted mid-run) can never look "already pulled" to a later
        # dedup check.
        tmp_target = target.with_name(target.name + ".tmp")
        shutil.rmtree(tmp_target, ignore_errors=True)
        with zipfile.ZipFile(zip_path) as zf:
            zf.extractall(tmp_target)
        if manifest is not None:
            (tmp_target / "manifest.json").write_text(
                json.dumps(manifest, indent=2, default=json_default),
                encoding="utf-8",
            )
        tmp_target.replace(target)
    else:
        shutil.move(str(zip_path), str(target))
        if manifest is not None:
            target.with_suffix(".manifest.json").write_text(
                json.dumps(manifest, indent=2, default=json_default),
                encoding="utf-8",
            )


def obb_names(package: str, version_code: int) -> list[str]:
    return [
        f"main.{version_code}.{package}.obb",
        f"patch.{version_code}.{package}.obb",
    ]


def staging_root(dest_root: Path) -> Path:
    """Where every in-progress (package, version_code) group's staged raw
    pulls live until they're merged. Wiped best-effort at the start of every
    :func:`apkpull.orchestrator.run` call, in case a previous run crashed
    before cleaning up its own groups."""
    return dest_root / _STAGING_DIRNAME


def group_staging_dir(dest_root: Path, package: str, version_code: int) -> Path:
    return staging_root(dest_root) / f"{package}-{version_code}"


def _device_staging_dir(
    dest_root: Path, package: str, version_code: int, device_id: str
) -> Path:
    # Wireless-adb device ids look like "192.168.1.5:5555" — sanitize rather
    # than assume every adb-reported id is filesystem-safe as-is.
    safe_id = device_id.replace(":", "_").replace("/", "_")
    return group_staging_dir(dest_root, package, version_code) / safe_id


@dataclass(slots=True)
class RawPull:
    """One device's raw contribution toward a ``(package, version_code)``
    bundle — pulled off one device and staged locally; not yet merged with
    any other device's contribution or assembled into a bundle."""

    device_id: str
    package: str
    version_code: int
    """``dumpsys``-reported version code — the grouping/target-path key."""
    version_name: str | None
    target: Path
    """:func:`target_path` result — known even before anything is built."""
    already_built: bool
    """``True``: ``target`` already existed before this pull (from an
    earlier, separate apkpull invocation); nothing below is populated
    except ``obb_pulled_files``."""
    base_path: Path | None = None
    split_paths: list[Path] = field(default_factory=list)
    obb_paths: list[Path] = field(default_factory=list)
    """Staged, not yet in their final location — only populated when
    ``already_built`` is ``False``."""
    obb_pulled_files: list[PulledFile] = field(default_factory=list)
    """Already in their final location — only populated when
    ``already_built`` is ``True``."""
    staging_dir: Path | None = None


class Puller:
    def __init__(self, device: Device, dest_root: Path) -> None:
        self.device = device
        self.dest_root = dest_root

    def pull_raw(
        self,
        package: str,
        *,
        output_format: OutputFormat = OutputFormat.APKS,
        report: PackageReporter | None = None,
    ) -> RawPull:
        """Pull this device's base.apk + splits (+ any OBB) off it and stage
        them locally — but don't build a bundle. That happens once, later,
        across every targeted device's ``RawPull`` for this
        (package, version_code) — see :func:`build_merged_bundle`.
        """
        self.device.ensure_connected()

        version_code = self.device.version_code(package)
        version_name = self.device.version_name(package)
        if version_code is None:
            raise PullError(f"Could not determine the installed version of {package}.")

        self.dest_root.mkdir(parents=True, exist_ok=True)
        target = target_path(self.dest_root, package, version_code, output_format)

        if target.exists():
            # Fast path: a *separate, earlier* apkpull invocation already
            # built this bundle. This can never be a sibling device from
            # this same run — merging only ever happens once, after every
            # targeted device's thread has already finished, so nothing in
            # this run could have written `target` yet. Still worth
            # re-checking OBBs even though the bundle itself is done — e.g.
            # a previous run with a different --format wouldn't have had
            # anywhere to put one for FOLDER output.
            logger.info(
                "[%s] %s already exists, skipping pull.", self.device.label, target.name
            )
            obb_dir = target if output_format == OutputFormat.FOLDER else self.dest_root
            obb_pulled: list[PulledFile] = []
            for obb_name in obb_names(package, version_code):
                remote_obb = f"/sdcard/Android/obb/{package}/{obb_name}"
                if self.device.file_exists(remote_obb):
                    obb_pulled.append(
                        self._pull_if_missing(
                            remote_obb, obb_dir / obb_name, FileKind.OBB, obb_name
                        )
                    )
            return RawPull(
                device_id=self.device.device_id,
                package=package,
                version_code=version_code,
                version_name=version_name,
                target=target,
                already_built=True,
                obb_pulled_files=obb_pulled,
            )

        remote_paths = self.device.apk_paths(package)
        if not remote_paths:
            raise PullError(f"Unable to get apk paths for {package}.")

        staging_dir = _device_staging_dir(
            self.dest_root, package, version_code, self.device.device_id
        )
        staging_dir.mkdir(parents=True, exist_ok=True)
        base_local, split_locals = self._pull_apks(
            package, remote_paths, staging_dir, report
        )

        try:
            ApkFile(base_local)
        except InvalidApkError as exc:
            raise PullError(f"Pulled base apk for {package} is invalid: {exc}") from exc

        obb_paths: list[Path] = []
        for obb_name in obb_names(package, version_code):
            remote_obb = f"/sdcard/Android/obb/{package}/{obb_name}"
            if self.device.file_exists(remote_obb):
                local_obb = staging_dir / obb_name
                size_human = self.device.remote_size_human(remote_obb)
                logger.info(
                    "[%s] Pulling %s (%s)...", self.device.label, obb_name, size_human
                )
                if report:
                    report(package, Stage.PULLING, f"{obb_name} ({size_human})")
                self.device.adb.pull(self.device.device_id, remote_obb, local_obb)
                obb_paths.append(local_obb)

        return RawPull(
            device_id=self.device.device_id,
            package=package,
            version_code=version_code,
            version_name=version_name,
            target=target,
            already_built=False,
            base_path=base_local,
            split_paths=split_locals,
            obb_paths=obb_paths,
            staging_dir=staging_dir,
        )

    def _pull_apks(
        self,
        package: str,
        remote_paths: list[str],
        dest_dir: Path,
        report: PackageReporter | None,
    ) -> tuple[Path, list[Path]]:
        base_local: Path | None = None
        split_locals: list[Path] = []
        for remote_path in remote_paths:
            # Android always names the base apk "base.apk" on-device; splits are named
            # "split_<name>.apk" — the "split_" prefix is redundant once zipped up
            # alongside base.apk, so it's dropped for a cleaner in-bundle filename.
            remote_name = Path(remote_path).name
            local_name = (
                remote_name.removeprefix("split_")
                if remote_name != BASE_NAME
                else remote_name
            )
            local_path = dest_dir / local_name
            size_human = self.device.remote_size_human(remote_path)
            logger.info(
                "[%s] Pulling %s (%s)...",
                self.device.label,
                local_path.name,
                size_human,
            )
            if report:
                report(package, Stage.PULLING, f"{local_path.name} ({size_human})")
            self.device.adb.pull(self.device.device_id, remote_path, local_path)
            if local_path.name == BASE_NAME:
                base_local = local_path
            else:
                split_locals.append(local_path)

        if base_local is None:
            raise PullError(f"{package} has no {BASE_NAME} among its installed paths.")
        return base_local, split_locals

    def _pull_if_missing(
        self, remote_path: str, local_path: Path, kind: FileKind, display_name: str
    ) -> PulledFile:
        if local_path.exists():
            logger.info(
                "[%s] %s already exists, skipping.", self.device.label, display_name
            )
            return PulledFile(
                kind=kind,
                name=display_name,
                local_path=local_path,
                size_bytes=local_path.stat().st_size,
                already_existed=True,
            )

        size_human = self.device.remote_size_human(remote_path)
        logger.info(
            "[%s] Pulling %s (%s)...", self.device.label, display_name, size_human
        )
        self.device.adb.pull(self.device.device_id, remote_path, local_path)
        return PulledFile(
            kind=kind,
            name=display_name,
            local_path=local_path,
            size_bytes=local_path.stat().st_size,
        )


def _place_obbs(
    contributions: list[RawPull],
    dest_root: Path,
    target: Path,
    output_format: OutputFormat,
) -> list[PulledFile]:
    """Union every contribution's staged OBBs by filename and copy each into
    its final location (disk→disk — these were already pulled off a device
    by :meth:`Puller.pull_raw`, no adb involved here)."""
    obb_dir = target if output_format == OutputFormat.FOLDER else dest_root
    obbs: dict[str, Path] = {}
    for contribution in contributions:
        for obb_path in contribution.obb_paths:
            obbs.setdefault(obb_path.name, obb_path)

    placed: list[PulledFile] = []
    for name in sorted(obbs):
        source = obbs[name]
        local_path = obb_dir / name
        if local_path.exists():
            placed.append(
                PulledFile(
                    kind=FileKind.OBB,
                    name=name,
                    local_path=local_path,
                    size_bytes=local_path.stat().st_size,
                    already_existed=True,
                )
            )
        else:
            local_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, local_path)
            placed.append(
                PulledFile(
                    kind=FileKind.OBB,
                    name=name,
                    local_path=local_path,
                    size_bytes=local_path.stat().st_size,
                )
            )
    return placed


def build_merged_bundle(
    package: str,
    version_code: int,
    contributions: list[RawPull],
    dest_root: Path,
    *,
    output_format: OutputFormat = OutputFormat.APKS,
    verify: bool = True,
    strict: bool = False,
    full: bool = False,
) -> tuple[Path, list[PulledFile]]:
    """Merge every contribution's base+splits (and OBBs) into one bundle.

    ``contributions`` must all be non-``already_built`` :class:`RawPull`\\ s
    for the same ``(package, version_code)``, and must already be sorted by
    ``device_id`` by the caller — that's what makes the pick deterministic
    on a split-filename collision between two devices (first sorted
    device_id wins; see step 2 below).

    ``full`` is forwarded to :func:`~apkpull.bundle.verify_bundle` (only
    meaningful when ``verify`` is also true, same as apkfile's own
    ``as_dict(full=...)`` it wraps) and controls how much detail the written
    ``manifest.json``/``.manifest.json`` sidecar carries.

    Pure aside from the filesystem: no adb, no threading — safe to call
    from anywhere once every contributing device's raw pull has finished.
    """
    target = target_path(dest_root, package, version_code, output_format)
    if target.exists():
        # Belt-and-suspenders: a *different, concurrently running* apkpull
        # process (not a sibling device thread from this run — see
        # pull_raw's docstring) could have built this since our
        # contributors were staged. Pre-existing race class, not new; just
        # don't clobber it.
        pulled = [
            PulledFile(
                kind=FileKind.BUNDLE,
                name=target.name,
                local_path=target,
                size_bytes=size_of(target),
                already_existed=True,
            )
        ]
        pulled.extend(_place_obbs(contributions, dest_root, target, output_format))
        return target, pulled

    primary = contributions[0]
    assert (
        primary.base_path is not None
    )  # only non-already_built contributions reach here

    # First occurrence (i.e. earliest sorted device_id) wins on a filename
    # collision — same split filename for the same version_code should mean
    # identical content across devices, since Play Store's split naming is
    # deterministic per abi/density/lang/version, so this is a tiebreaker
    # for a case that shouldn't meaningfully arise, not a real conflict
    # resolution.
    splits: dict[str, Path] = {}
    for contribution in contributions:
        for split_path in contribution.split_paths:
            splits.setdefault(split_path.name, split_path)
    split_paths = [splits[name] for name in sorted(splits)]

    with tempfile.TemporaryDirectory(prefix="apkpull-merge-") as tmp:
        zip_path = Path(tmp) / "bundle.apks"
        logger.info("Packaging %s from %d device(s)...", package, len(contributions))
        # Base.apk isn't device-split — Play Store serves one canonical build per
        # version_code — so no cross-device reconciliation is needed here; the manifest
        # fields (package, version_code, ...) are derived from `primary.base_path` alone.
        build_apks_bundle(
            base_path=primary.base_path,
            split_paths=split_paths,
            dest_path=zip_path,
        )

        verified = None
        manifest = None
        if verify:
            verified, manifest = verify_bundle(
                package, version_code, zip_path, strict=strict, full=full
            )

        materialize(zip_path, target, output_format, manifest)

    pulled = [
        PulledFile(
            kind=FileKind.BUNDLE,
            name=target.name,
            local_path=target,
            size_bytes=size_of(target),
            verified=verified,
        )
    ]
    pulled.extend(_place_obbs(contributions, dest_root, target, output_format))
    return target, pulled
