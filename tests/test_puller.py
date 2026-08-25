import json
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from apkfile import InvalidApkError

from apkpull.device import Device
from apkpull.exceptions import DeviceDisconnectedError, PullError, VerificationError
from apkpull.models import FileKind, OutputFormat, PulledFile
from apkpull.puller import Puller, build_merged_bundle, size_of

from .helpers import FakeAdb


def make_puller(
    tmp_path, *, pm_path, dumpsys, obb_exists=(), connected=True, device_id="fake-1"
):
    adb = FakeAdb()
    adb.connected = connected
    adb.shell_responses["dumpsys package com.app"] = dumpsys
    adb.shell_responses["pm path com.app"] = pm_path
    for remote in obb_exists:
        adb.shell_responses[f"test -f '{remote}' && echo yes"] = "yes\n"
    device = Device(adb, device_id)
    return Puller(device, tmp_path), adb


def fake_apk(
    *, version_code=41, version_name="1.0", min_sdk=21, target_sdk=34, labels=None
):
    return SimpleNamespace(
        labels=labels or {"": "App"},
        version_code=version_code,
        version_name=version_name,
        min_sdk_version=min_sdk,
        target_sdk_version=target_sdk,
    )


def _patched_apk_file(**kwargs):
    return patch("apkpull.puller.ApkFile", return_value=fake_apk(**kwargs))


def pull_and_build(puller, package, **kwargs):
    """Reconstructs ``pull_package()``'s old all-in-one return shape
    (``dest, files, version_code, version_name``) from the new two-step
    pull_raw()/build_merged_bundle() pipeline, for tests that don't care
    about multi-device merging specifically -- a single contribution merges
    trivially to the same result the old single-device path produced."""
    output_format = kwargs.get("output_format", OutputFormat.APKS)
    verify = kwargs.get("verify", True)
    strict = kwargs.get("strict", False)
    full = kwargs.get("full", False)
    report = kwargs.get("report")
    raw = puller.pull_raw(package, output_format=output_format, report=report)
    if raw.already_built:
        files = [
            PulledFile(
                kind=FileKind.BUNDLE,
                name=raw.target.name,
                local_path=raw.target,
                size_bytes=size_of(raw.target),
                already_existed=True,
            ),
            *raw.obb_pulled_files,
        ]
        return raw.target, files, raw.version_code, raw.version_name
    target, files = build_merged_bundle(
        package,
        raw.version_code,
        [raw],
        puller.dest_root,
        output_format=output_format,
        verify=verify,
        strict=strict,
        full=full,
    )
    return target, files, raw.version_code, raw.version_name


# -- basic pulling / bundling (verification disabled — that's covered separately below) ------


def test_single_apk_pull_produces_a_named_bundle(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=3.1.11",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with _patched_apk_file(version_code=41, version_name="3.1.11"):
        dest, files, vcode, vname = pull_and_build(puller, "com.app", verify=False)

    assert vcode == 41
    assert vname == "3.1.11"
    assert len(files) == 1
    bundle = files[0]
    assert bundle.kind == FileKind.BUNDLE
    assert bundle.name == "com.app-41.apks"
    assert bundle.local_path == tmp_path / "com.app-41.apks"
    assert dest == bundle.local_path
    assert bundle.local_path.is_file()
    assert bundle.verified is None  # verify=False: never checked

    with zipfile.ZipFile(bundle.local_path) as zf:
        assert set(zf.namelist()) == {"base.apk", "meta.sai_v2.json"}
        meta = json.loads(zf.read("meta.sai_v2.json"))
        # com.app/41 is dumpsys-reported and drives the target *filename* --
        # meta.sai_v2.json's package/version_code come from re-parsing the
        # real base.apk bytes FakeAdb staged (politedroid.apk, by default).
        assert meta["package"] == "com.politedroid"
        assert meta["version_code"] == 4


def test_split_apk_pull_bundles_base_and_splits_into_one_apks_file(
    tmp_path, force_splits
):
    pm_path = (
        "package:/data/app/com.app-x/base.apk\n"
        "package:/data/app/com.app-x/split_config.arm64_v8a.apk\n"
        "package:/data/app/com.app-x/split_config.en.apk\n"
    )
    puller, _adb = make_puller(
        tmp_path, dumpsys="versionCode=451 versionName=1.0", pm_path=pm_path
    )
    with (
        _patched_apk_file(version_code=451),
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        _, files, _, _ = pull_and_build(puller, "com.app", verify=False)

    bundle = files[0]
    assert bundle.name == "com.app-451.apks"
    with zipfile.ZipFile(bundle.local_path) as zf:
        assert set(zf.namelist()) == {
            "base.apk",
            "config.arm64_v8a.apk",
            "config.en.apk",
            "meta.sai_v2.json",
        }


def test_obb_files_pulled_when_present(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    with _patched_apk_file(version_code=7):
        _, files, _, _ = pull_and_build(puller, "com.app", verify=False)
    obb_files = [f for f in files if f.kind == FileKind.OBB]
    assert len(obb_files) == 1
    assert obb_files[0].name == "main.7.com.app.obb"
    assert obb_files[0].local_path == tmp_path / "main.7.com.app.obb"


def test_no_obb_files_when_absent(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with _patched_apk_file(version_code=7):
        _, files, _, _ = pull_and_build(puller, "com.app", verify=False)
    assert not any(f.kind == FileKind.OBB for f in files)


def test_skips_pull_when_bundle_already_exists(tmp_path):
    puller, adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    existing = tmp_path / "com.app-41.apks"
    existing.write_bytes(b"already-here")

    _, files, _, _ = pull_and_build(puller, "com.app", verify=False)
    assert files[0].already_existed is True
    assert (
        files[0].local_path.read_bytes() == b"already-here"
    )  # not overwritten/rebuilt
    assert not adb.pulled  # adb pull was never invoked


def test_raises_when_version_code_unavailable(tmp_path):
    puller, _adb = make_puller(
        tmp_path, dumpsys="no matching output", pm_path="package:/data/app/base.apk\n"
    )
    with pytest.raises(PullError):
        pull_and_build(puller, "com.app", verify=False)


def test_raises_when_no_apk_paths(tmp_path):
    puller, _adb = make_puller(
        tmp_path, dumpsys="versionCode=1 versionName=1.0", pm_path=""
    )
    with pytest.raises(PullError):
        pull_and_build(puller, "com.app", verify=False)


def test_raises_when_base_apk_fails_to_parse(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=1 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with (
        patch("apkpull.puller.ApkFile", side_effect=InvalidApkError("corrupt")),
        pytest.raises(PullError),
    ):
        pull_and_build(puller, "com.app", verify=False)


def test_raises_when_device_disconnected_before_pulling(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=1 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
        connected=False,
    )
    with pytest.raises(DeviceDisconnectedError):
        pull_and_build(puller, "com.app", verify=False)


# -- pull_raw() in isolation -----------------------------------------------------------------


def test_pull_raw_stages_base_and_splits_without_building_a_bundle(tmp_path):
    pm_path = (
        "package:/data/app/com.app-x/base.apk\n"
        "package:/data/app/com.app-x/split_config.en.apk\n"
    )
    puller, _adb = make_puller(
        tmp_path, dumpsys="versionCode=41 versionName=1.0", pm_path=pm_path
    )
    with _patched_apk_file(version_code=41):
        raw = puller.pull_raw("com.app")

    assert raw.already_built is False
    assert not raw.target.exists()  # nothing built yet
    assert (
        raw.base_path
        == tmp_path / ".apkpull-staging" / "com.app-41" / "fake-1" / "base.apk"
    )
    assert raw.base_path.is_file()
    assert [p.name for p in raw.split_paths] == ["config.en.apk"]


def test_pull_raw_short_circuits_when_target_already_exists(tmp_path):
    puller, adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    (tmp_path / "com.app-41.apks").write_bytes(b"already-here")

    raw = puller.pull_raw("com.app")
    assert raw.already_built is True
    assert not adb.pulled


def test_pull_raw_still_pulls_missing_obb_when_target_already_exists(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    (tmp_path / "com.app-7.apks").write_bytes(b"already-here")

    raw = puller.pull_raw("com.app")
    assert raw.already_built is True
    assert len(raw.obb_pulled_files) == 1
    assert raw.obb_pulled_files[0].local_path == tmp_path / "main.7.com.app.obb"
    assert raw.obb_pulled_files[0].local_path.is_file()


def test_pull_raw_stages_obb_into_staging_when_bundle_not_yet_built(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    with _patched_apk_file(version_code=7):
        raw = puller.pull_raw("com.app")

    assert raw.already_built is False
    assert len(raw.obb_paths) == 1
    assert raw.obb_paths[0].parent == raw.staging_dir
    assert not (tmp_path / "main.7.com.app.obb").exists()  # not placed yet


# -- build_merged_bundle() -- the core cross-device merge surface ---------------------------


def test_build_merged_bundle_unions_distinct_splits_from_two_devices(
    tmp_path, force_splits
):
    """The direct regression test for the reported bug: two devices with
    genuinely different splits (arm64/en vs x86_64/he) must both end up in
    the final bundle, not just whichever pulled first."""
    puller_a, _adb_a = make_puller(
        tmp_path,
        device_id="dev-a",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path=(
            "package:/data/app/x/base.apk\n"
            "package:/data/app/x/split_config.arm64_v8a.apk\n"
            "package:/data/app/x/split_config.en.apk\n"
        ),
    )
    puller_b, _adb_b = make_puller(
        tmp_path,
        device_id="dev-b",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path=(
            "package:/data/app/x/base.apk\n"
            "package:/data/app/x/split_config.x86_64.apk\n"
            "package:/data/app/x/split_config.he.apk\n"
        ),
    )
    with _patched_apk_file(version_code=100):
        raw_a = puller_a.pull_raw("com.app")
        raw_b = puller_b.pull_raw("com.app")

    with force_splits(where=lambda p: p.name != "base.apk"):
        target, files = build_merged_bundle(
            "com.app",
            100,
            sorted([raw_a, raw_b], key=lambda r: r.device_id),
            tmp_path,
            verify=False,
        )

    bundle = next(f for f in files if f.kind == FileKind.BUNDLE)
    assert bundle.local_path == target
    with zipfile.ZipFile(target) as zf:
        names = set(zf.namelist())
    assert {
        "base.apk",
        "config.arm64_v8a.apk",
        "config.en.apk",
        "config.x86_64.apk",
        "config.he.apk",
        "meta.sai_v2.json",
    } <= names


def test_build_merged_bundle_dedupes_identical_split_filenames_keeping_first_by_device_id(
    tmp_path, force_splits, politedroid_bytes, test_debug_bytes
):
    puller_a, adb_a = make_puller(
        tmp_path,
        device_id="dev-a",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path=(
            "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.en.apk\n"
        ),
    )
    puller_b, adb_b = make_puller(
        tmp_path,
        device_id="dev-b",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path=(
            "package:/data/app/x/base.apk\npackage:/data/app/x/split_config.en.apk\n"
        ),
    )
    # Shouldn't happen for a real device pair (same split filename should mean
    # identical content for the same version_code) -- using two different real
    # fixture apks here is only to prove which contribution wins the tiebreak
    # (the winner's actual split bytes end up in the bundle).
    adb_a.split_apk_bytes = politedroid_bytes
    adb_b.split_apk_bytes = test_debug_bytes
    with _patched_apk_file(version_code=100):
        raw_a = puller_a.pull_raw("com.app")
        raw_b = puller_b.pull_raw("com.app")

    with force_splits(where=lambda p: p.name != "base.apk"):
        target, _files = build_merged_bundle(
            "com.app",
            100,
            sorted([raw_a, raw_b], key=lambda r: r.device_id),
            tmp_path,
            verify=False,
        )
    with zipfile.ZipFile(target) as zf:
        assert zf.namelist().count("config.en.apk") == 1
        assert zf.read("config.en.apk") == politedroid_bytes  # dev-a sorts first


def test_build_merged_bundle_uses_first_devices_base_apk_and_metadata(
    tmp_path, politedroid_bytes, test_debug_bytes
):
    """ApksFile.create() derives the whole manifest by re-parsing whichever
    contribution's base_path is primary -- there's no separate metadata
    channel to inject fake per-device values into anymore, so this proves
    the real post-refactor behavior with two genuinely different base apks:
    dev-a's actual base-apk content is what ends up in the bundle."""
    puller_a, adb_a = make_puller(
        tmp_path,
        device_id="dev-a",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
    )
    puller_b, adb_b = make_puller(
        tmp_path,
        device_id="dev-b",
        dumpsys="versionCode=100 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
    )
    adb_a.base_apk_bytes = politedroid_bytes
    adb_b.base_apk_bytes = test_debug_bytes
    raw_a = puller_a.pull_raw("com.app")
    raw_b = puller_b.pull_raw("com.app")

    target, _files = build_merged_bundle(
        "com.app",
        100,
        sorted([raw_a, raw_b], key=lambda r: r.device_id),
        tmp_path,
        verify=False,
    )
    with zipfile.ZipFile(target) as zf:
        meta = json.loads(zf.read("meta.sai_v2.json"))
    # dev-a sorts first, so its base apk's real content wins
    assert meta["label"] == "Polite Droid"
    assert meta["package"] == "com.politedroid"


def test_build_merged_bundle_unions_obbs_from_multiple_devices_without_duplicate_copy(
    tmp_path,
):
    puller_a, _adb_a = make_puller(
        tmp_path,
        device_id="dev-a",
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    puller_b, _adb_b = make_puller(
        tmp_path,
        device_id="dev-b",
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    with _patched_apk_file(version_code=7):
        raw_a = puller_a.pull_raw("com.app")
        raw_b = puller_b.pull_raw("com.app")

    _target, files = build_merged_bundle(
        "com.app",
        7,
        sorted([raw_a, raw_b], key=lambda r: r.device_id),
        tmp_path,
        verify=False,
    )
    obb_files = [f for f in files if f.kind == FileKind.OBB]
    assert len(obb_files) == 1
    assert obb_files[0].local_path == tmp_path / "main.7.com.app.obb"


def test_build_merged_bundle_short_circuits_when_target_already_exists(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
    )
    with _patched_apk_file(version_code=41):
        raw = puller.pull_raw("com.app")
    # A *different, concurrently running* apkpull process finished first --
    # not a sibling device from this same run (that can't happen: merging
    # only ever happens after every targeted device has already resolved).
    (tmp_path / "com.app-41.apks").write_bytes(b"already-here")

    with patch("apkpull.puller.build_apks_bundle") as mock_build:
        target, files = build_merged_bundle(
            "com.app", 41, [raw], tmp_path, verify=False
        )
    mock_build.assert_not_called()
    assert files[0].already_existed is True
    assert target.read_bytes() == b"already-here"


def test_build_merged_bundle_raises_verification_error_in_strict_mode_and_leaves_no_target(
    tmp_path,
):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
    )
    with _patched_apk_file(version_code=41):
        raw = puller.pull_raw("com.app")

    with (
        patch(
            "apkpull.puller.verify_bundle", side_effect=VerificationError("mismatch")
        ),
        pytest.raises(VerificationError),
    ):
        build_merged_bundle("com.app", 41, [raw], tmp_path, verify=True, strict=True)
    assert not (tmp_path / "com.app-41.apks").exists()


def test_build_merged_bundle_folder_format_places_unioned_obb_inside_extracted_folder(
    tmp_path,
):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    with _patched_apk_file(version_code=7):
        raw = puller.pull_raw("com.app")

    target, files = build_merged_bundle(
        "com.app", 7, [raw], tmp_path, output_format=OutputFormat.FOLDER, verify=False
    )
    obb = next(f for f in files if f.kind == FileKind.OBB)
    assert obb.local_path == target / "main.7.com.app.obb"
    assert obb.local_path.is_file()


def test_build_merged_bundle_zip_format_same_contents_as_apks(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/x/base.apk\n",
    )
    with _patched_apk_file(version_code=41):
        raw = puller.pull_raw("com.app")

    target, _files = build_merged_bundle(
        "com.app", 41, [raw], tmp_path, output_format=OutputFormat.ZIP, verify=False
    )
    assert target.name == "com.app-41.zip"
    with zipfile.ZipFile(target) as zf:
        assert "meta.sai_v2.json" in zf.namelist()


# -- verification integration --------------------------------------------------------------


def test_verify_true_invokes_verify_bundle_and_writes_sidecar_manifest(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    manifest = {"package_name": "com.app", "version_code": 41}
    with (
        _patched_apk_file(version_code=41),
        patch(
            "apkpull.puller.verify_bundle", return_value=(True, manifest)
        ) as mock_verify,
    ):
        _, files, _, _ = pull_and_build(puller, "com.app")

    mock_verify.assert_called_once()
    assert files[0].verified is True
    manifest_path = tmp_path / "com.app-41.manifest.json"
    assert manifest_path.is_file()
    assert json.loads(manifest_path.read_text()) == manifest


def test_verify_false_skips_verify_bundle_entirely(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with (
        _patched_apk_file(version_code=41),
        patch("apkpull.puller.verify_bundle") as mock_verify,
    ):
        _, files, _, _ = pull_and_build(puller, "com.app", verify=False)

    mock_verify.assert_not_called()
    assert files[0].verified is None
    assert not (tmp_path / "com.app-41.manifest.json").exists()


def test_strict_flag_forwarded_to_verify_bundle(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with (
        _patched_apk_file(version_code=41),
        patch("apkpull.puller.verify_bundle", return_value=(True, {})) as mock_verify,
    ):
        pull_and_build(puller, "com.app", strict=True)
    _, kwargs = mock_verify.call_args
    assert kwargs["strict"] is True


def test_full_flag_forwarded_to_verify_bundle(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with (
        _patched_apk_file(version_code=41),
        patch("apkpull.puller.verify_bundle", return_value=(True, {})) as mock_verify,
    ):
        pull_and_build(puller, "com.app", full=True)
    _, kwargs = mock_verify.call_args
    assert kwargs["full"] is True


def test_full_flag_defaults_to_false(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with (
        _patched_apk_file(version_code=41),
        patch("apkpull.puller.verify_bundle", return_value=(True, {})) as mock_verify,
    ):
        pull_and_build(puller, "com.app")
    _, kwargs = mock_verify.call_args
    assert kwargs["full"] is False


# -- output formats -------------------------------------------------------------------------


def test_zip_format_uses_zip_extension_same_contents(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    with _patched_apk_file(version_code=41):
        _, files, _, _ = pull_and_build(
            puller, "com.app", output_format=OutputFormat.ZIP, verify=False
        )

    bundle = files[0]
    assert bundle.name == "com.app-41.zip"
    assert bundle.local_path == tmp_path / "com.app-41.zip"
    with zipfile.ZipFile(bundle.local_path) as zf:
        assert "meta.sai_v2.json" in zf.namelist()


def test_folder_format_extracts_loose_files(tmp_path, force_splits):
    pm_path = (
        "package:/data/app/com.app-x/base.apk\n"
        "package:/data/app/com.app-x/split_config.en.apk\n"
    )
    puller, _adb = make_puller(
        tmp_path, dumpsys="versionCode=41 versionName=1.0", pm_path=pm_path
    )
    manifest = {"package_name": "com.app", "version_code": 41}
    with (
        _patched_apk_file(version_code=41),
        patch("apkpull.puller.verify_bundle", return_value=(True, manifest)),
        force_splits(where=lambda p: p.name != "base.apk"),
    ):
        _, files, _, _ = pull_and_build(
            puller, "com.app", output_format=OutputFormat.FOLDER
        )

    bundle = files[0]
    assert bundle.name == "com.app-41"
    folder = tmp_path / "com.app-41"
    assert bundle.local_path == folder
    assert folder.is_dir()
    assert (folder / "base.apk").is_file()
    assert (folder / "config.en.apk").is_file()
    assert json.loads((folder / "manifest.json").read_text()) == manifest
    assert not folder.with_name(
        folder.name + ".tmp"
    ).exists()  # atomic rename left no residue
    assert bundle.size_bytes == sum(
        f.stat().st_size for f in folder.iterdir() if f.is_file()
    )


def test_folder_format_dedup_skips_existing_folder(tmp_path):
    puller, adb = make_puller(
        tmp_path,
        dumpsys="versionCode=41 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
    )
    existing = tmp_path / "com.app-41"
    existing.mkdir()
    (existing / "base.apk").write_bytes(b"already-here")

    _, files, _, _ = pull_and_build(
        puller, "com.app", output_format=OutputFormat.FOLDER, verify=False
    )
    assert files[0].already_existed is True
    assert not adb.pulled


def test_folder_format_places_obb_inside_folder(tmp_path):
    puller, _adb = make_puller(
        tmp_path,
        dumpsys="versionCode=7 versionName=1.0",
        pm_path="package:/data/app/com.app-x/base.apk\n",
        obb_exists=["/sdcard/Android/obb/com.app/main.7.com.app.obb"],
    )
    with _patched_apk_file(version_code=7):
        _, files, _, _ = pull_and_build(
            puller, "com.app", output_format=OutputFormat.FOLDER, verify=False
        )

    obb = next(f for f in files if f.kind == FileKind.OBB)
    assert obb.local_path == tmp_path / "com.app-7" / "main.7.com.app.obb"
    assert obb.local_path.is_file()
