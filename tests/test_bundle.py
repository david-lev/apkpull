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


def test_build_apks_bundle_writes_base_and_splits_and_meta(tmp_path):
    base = tmp_path / "base.apk"
    base.write_bytes(b"base-bytes")
    split = tmp_path / "config.en.apk"
    split.write_bytes(b"split-bytes")
    dest = tmp_path / "out.apks"

    build_apks_bundle(
        base_path=base,
        split_paths=[split],
        dest_path=dest,
        package="com.app",
        version_code=5,
        version_name="1.0",
        app_name="App",
        min_sdk_version=21,
        target_sdk_version=34,
    )

    assert dest.is_file()
    assert not dest.with_name(dest.name + ".tmp").exists()
    with zipfile.ZipFile(dest) as zf:
        assert set(zf.namelist()) == {BASE_NAME, "config.en.apk", META_NAME}
        assert zf.read(BASE_NAME) == b"base-bytes"
        assert zf.read("config.en.apk") == b"split-bytes"
        meta = json.loads(zf.read(META_NAME))
    assert meta == {
        "package": "com.app",
        "label": "App",
        "version_code": 5,
        "meta_version": 2,
        "version_name": "1.0",
        "min_sdk": 21,
        "target_sdk": 34,
    }


def test_build_apks_bundle_omits_none_optional_fields(tmp_path):
    base = tmp_path / "base.apk"
    base.write_bytes(b"x")
    dest = tmp_path / "out.apks"

    build_apks_bundle(
        base_path=base,
        split_paths=[],
        dest_path=dest,
        package="com.app",
        version_code=1,
        version_name=None,
        app_name="App",
        min_sdk_version=None,
        target_sdk_version=None,
    )

    with zipfile.ZipFile(dest) as zf:
        meta = json.loads(zf.read(META_NAME))
    assert meta.keys() == {"package", "label", "version_code", "meta_version"}


def test_build_apks_bundle_replaces_existing_file_atomically(tmp_path):
    base = tmp_path / "base.apk"
    base.write_bytes(b"new")
    dest = tmp_path / "out.apks"
    dest.write_bytes(b"stale-content")

    build_apks_bundle(
        base_path=base,
        split_paths=[],
        dest_path=dest,
        package="com.app",
        version_code=1,
        version_name=None,
        app_name="App",
        min_sdk_version=None,
        target_sdk_version=None,
    )
    with zipfile.ZipFile(dest) as zf:
        assert zf.read(BASE_NAME) == b"new"


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
