import enum
import json
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from apkfile import InvalidApkError, InvalidBundleError

from apkpull.bundle import (
    BASE_NAME,
    META_NAME,
    build_apks_bundle,
    json_default,
    verify_bundle,
)
from apkpull.exceptions import VerificationError


def test_build_apks_bundle_writes_base_and_meta(tmp_path, politedroid_bytes):
    base = tmp_path / "base.apk"
    base.write_bytes(politedroid_bytes)
    dest = tmp_path / "out.apks"

    build_apks_bundle(base_path=base, split_paths=[], dest_path=dest)

    assert dest.is_file()
    assert not dest.with_name(dest.name + ".tmp").exists()
    with zipfile.ZipFile(dest) as zf:
        assert set(zf.namelist()) == {BASE_NAME, META_NAME}
        assert zf.read(BASE_NAME) == politedroid_bytes
        meta = json.loads(zf.read(META_NAME))
    assert meta == {
        "package": "com.politedroid",
        "label": "Polite Droid",
        "version_code": 4,
        "meta_version": 2,
        "version_name": "1.3",
        "min_sdk": 3,
    }


def test_build_apks_bundle_writes_splits_under_their_original_filenames(
    tmp_path, politedroid_bytes, test_debug_bytes, force_splits
):
    base = tmp_path / "base.apk"
    base.write_bytes(politedroid_bytes)
    split = tmp_path / "config.en.apk"
    split.write_bytes(test_debug_bytes)
    dest = tmp_path / "out.apks"

    with force_splits(split):
        build_apks_bundle(base_path=base, split_paths=[split], dest_path=dest)

    with zipfile.ZipFile(dest) as zf:
        assert set(zf.namelist()) == {BASE_NAME, "config.en.apk", META_NAME}
        assert zf.read("config.en.apk") == test_debug_bytes


def test_build_apks_bundle_omits_none_optional_fields(tmp_path, test_debug_bytes):
    base = tmp_path / "base.apk"
    base.write_bytes(test_debug_bytes)
    dest = tmp_path / "out.apks"

    build_apks_bundle(base_path=base, split_paths=[], dest_path=dest)

    with zipfile.ZipFile(dest) as zf:
        meta = json.loads(zf.read(META_NAME))
    # test-debug.apk has no min_sdk/target_sdk in its manifest
    assert meta.keys() == {
        "package",
        "label",
        "version_code",
        "meta_version",
        "version_name",
    }


def test_build_apks_bundle_replaces_existing_file_atomically(
    tmp_path, politedroid_bytes
):
    base = tmp_path / "base.apk"
    base.write_bytes(politedroid_bytes)
    dest = tmp_path / "out.apks"
    dest.write_bytes(b"stale-content")

    build_apks_bundle(base_path=base, split_paths=[], dest_path=dest)

    with zipfile.ZipFile(dest) as zf:
        assert zf.read(BASE_NAME) == politedroid_bytes


def _apks_ctor(*, package_name="com.app", version_code=5, splits=()):
    mock = SimpleNamespace(
        package_name=package_name,
        version_code=version_code,
        as_dict=lambda *, full=False: {
            "package_name": package_name,
            "version_code": version_code,
            "full": full,
        },
        splits=splits,
    )
    return mock


def test_verify_bundle_true_when_matching():
    with patch(
        "apkpull.bundle.ApksFile",
        return_value=_apks_ctor(splits=[SimpleNamespace(split_name="config.en")]),
    ):
        verified, manifest = verify_bundle("com.app", 5, Path("x.apks"))
    assert verified is True
    assert manifest["splits"] == ["config.en"]
    assert manifest["verified"] is True


def test_verify_bundle_full_defaults_to_false():
    with patch("apkpull.bundle.ApksFile", return_value=_apks_ctor()):
        _, manifest = verify_bundle("com.app", 5, Path("x.apks"))
    assert manifest["full"] is False


def test_verify_bundle_forwards_full_to_as_dict():
    with patch("apkpull.bundle.ApksFile", return_value=_apks_ctor()):
        _, manifest = verify_bundle("com.app", 5, Path("x.apks"), full=True)
    assert manifest["full"] is True


def test_verify_bundle_false_on_package_mismatch():
    with patch(
        "apkpull.bundle.ApksFile", return_value=_apks_ctor(package_name="com.other")
    ):
        verified, _ = verify_bundle("com.app", 5, Path("x.apks"))
    assert verified is False


def test_verify_bundle_false_on_version_mismatch():
    with patch("apkpull.bundle.ApksFile", return_value=_apks_ctor(version_code=99)):
        verified, _ = verify_bundle("com.app", 5, Path("x.apks"))
    assert verified is False


def test_verify_bundle_strict_raises_on_mismatch():
    with (
        patch(
            "apkpull.bundle.ApksFile", return_value=_apks_ctor(package_name="com.other")
        ),
        pytest.raises(VerificationError),
    ):
        verify_bundle("com.app", 5, Path("x.apks"), strict=True)


def test_verify_bundle_handles_unparseable_bundle_without_raising():
    with patch("apkpull.bundle.ApksFile", side_effect=InvalidBundleError("bad zip")):
        verified, manifest = verify_bundle("com.app", 5, Path("x.apks"))
    assert verified is False
    assert "error" in manifest


def test_verify_bundle_handles_invalid_apk_inside_bundle():
    with patch("apkpull.bundle.ApksFile", side_effect=InvalidApkError("bad apk")):
        verified, _manifest = verify_bundle("com.app", 5, Path("x.apks"))
    assert verified is False


def test_verify_bundle_strict_raises_when_unparseable():
    with (
        patch("apkpull.bundle.ApksFile", side_effect=InvalidBundleError("bad zip")),
        pytest.raises(VerificationError),
    ):
        verify_bundle("com.app", 5, Path("x.apks"), strict=True)


def test_json_default_uses_enum_value():
    class Color(enum.Enum):
        RED = "red"

    assert json_default(Color.RED) == "red"


def test_json_default_stringifies_other_types():
    assert json_default(Path("a/b")) == str(Path("a/b"))
