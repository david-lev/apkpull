"""Build and verify SAI/bundletool-compatible ``.apks`` bundles.

A ``.apks`` file (as produced by e.g. `SAI <https://github.com/Aefyr/SAI>`_) is
just a zip of a base apk + splits + a ``meta.sai_v2.json`` manifest —
``apkfile.ApksFile`` already knows how to read (and install) one. Producing
this instead of a loose directory of apk files turns a device pull into one
shareable, immediately-installable artifact per (package, version), openable
by apkfile itself, by SAI on another Android device, or by any zip tool.
"""

from __future__ import annotations

import enum
import logging
from pathlib import Path

from apkfile import ApksFile, InvalidApkError, InvalidBundleError

from .exceptions import VerificationError

logger = logging.getLogger("apkpull.bundle")

META_NAME = "meta.sai_v2.json"
BASE_NAME = "base.apk"


def build_apks_bundle(
    *,
    base_path: Path,
    split_paths: list[Path],
    dest_path: Path,
) -> None:
    """Build a SAI-format ``.apks`` file at ``dest_path`` from ``base_path`` + ``split_paths``.

    Delegates to ``apkfile.ApksFile.create``, which re-parses ``base_path`` to derive the
    ``meta.sai_v2.json`` manifest (package, label, version_code, version_name, min/target sdk).

    Written to a ``.tmp`` sibling first and atomically renamed into place, so
    a crash or interrupt mid-write can never leave a half-written file at
    ``dest_path`` for a later run's dedup check to mistake for a complete pull.
    """
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest_path.with_name(dest_path.name + ".tmp")
    try:
        built = ApksFile.create([base_path, *split_paths], tmp_path)
        built._zip.close()  # release the handle before the rename below (Windows can't rename an open file)
        tmp_path.replace(dest_path)
    finally:
        tmp_path.unlink(missing_ok=True)


def json_default(value):
    """``json.dumps(..., default=...)`` hook for the `Path`/`Enum` values ``ApksFile.as_dict()`` leaves as-is."""
    if isinstance(value, enum.Enum):
        return value.value
    return str(value)


def verify_bundle(
    package: str,
    expected_version_code: int,
    bundle_path: Path,
    *,
    strict: bool = False,
    full: bool = False,
) -> tuple[bool, dict]:
    """Re-open ``bundle_path`` with apkfile and confirm it matches ``package``/``expected_version_code``.

    This is what catches a truncated/corrupt pull, or a bundle-writing bug,
    that ``adb pull``'s own exit code and the zip step's own success missed.

    ``full`` is forwarded to ``ApksFile.as_dict()`` — trimmed (the default)
    leaves out several verbose/duplicative sections (full per-permission AOSP
    detail, exported/deep-link component lists, size breakdown, dex info,
    most certificate fields) to keep the written manifest digestible; ``full``
    includes all of it.

    Returns ``(verified, manifest_dict)``. Raises :class:`VerificationError` if
    ``strict`` and verification fails — including the bundle failing to parse
    at all.
    """
    try:
        apks = ApksFile(bundle_path)
    except (InvalidBundleError, InvalidApkError) as exc:
        logger.warning("Verification failed for %s: %s", bundle_path.name, exc)
        if strict:
            raise VerificationError(
                f"{bundle_path.name} failed to parse: {exc}"
            ) from exc
        return False, {"error": str(exc)}

    verified = (
        apks.package_name == package and apks.version_code == expected_version_code
    )
    if not verified:
        logger.warning(
            "Verification mismatch for %s: package=%s (expected %s) version=%s (expected %s)",
            bundle_path.name,
            apks.package_name,
            package,
            apks.version_code,
            expected_version_code,
        )

    manifest = apks.as_dict(full=full)
    manifest["splits"] = [s.split_name for s in apks.splits]
    manifest["verified"] = verified

    if strict and not verified:
        raise VerificationError(f"Verification failed for {bundle_path.name}")
    return verified, manifest
