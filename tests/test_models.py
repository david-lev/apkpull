import json

from apkpull.models import (
    DeviceInfo,
    DeviceOutcome,
    FileKind,
    PulledFile,
    RunSummary,
    Status,
)


def make_outcome(status: Status) -> DeviceOutcome:
    return DeviceOutcome(
        device=DeviceInfo(device_id="d1", model="Pixel"),
        package="com.app",
        status=status,
    )


def test_fingerprint_matches_for_devices_with_same_play_relevant_properties():
    a = DeviceInfo(
        device_id="d1",
        model="Pixel",
        abi="arm64-v8a",
        langs=frozenset({"en"}),
        sdk=34,
        density_bucket="xxhdpi",
    )
    b = DeviceInfo(
        device_id="d2",
        model="Pixel Clone",
        abi="arm64-v8a",
        langs=frozenset({"en"}),
        sdk=34,
        density_bucket="xxhdpi",
    )
    assert a.fingerprint == b.fingerprint


def test_fingerprint_differs_when_any_relevant_property_differs():
    a = DeviceInfo(
        device_id="d1",
        abi="arm64-v8a",
        langs=frozenset({"en"}),
        sdk=34,
        density_bucket="xxhdpi",
    )
    b = DeviceInfo(
        device_id="d2",
        abi="arm64-v8a",
        langs=frozenset({"en"}),
        sdk=33,
        density_bucket="xxhdpi",
    )
    assert a.fingerprint != b.fingerprint


def test_fingerprint_uses_density_bucket_not_raw_density():
    """Two devices reporting different raw dpi can still round to the same
    Android density bucket and get an identical dpi split from Play — so the
    fingerprint must compare buckets, not raw density, in both directions:
    same bucket despite different raw dpi is a match; different bucket is not,
    even if that's counterintuitive from the raw numbers alone."""
    a = DeviceInfo(
        device_id="d1", abi="arm64-v8a", density=420, density_bucket="xxhdpi"
    )
    b = DeviceInfo(
        device_id="d2", abi="arm64-v8a", density=440, density_bucket="xxhdpi"
    )
    assert a.fingerprint == b.fingerprint

    c = DeviceInfo(device_id="d3", abi="arm64-v8a", density=320, density_bucket="xhdpi")
    assert a.fingerprint != c.fingerprint


def test_fingerprint_uses_full_langs_set_not_just_primary_lang():
    """Two devices with the same primary `lang` but different secondary installed
    languages would get different Play Store language splits, so they must not
    fingerprint as duplicates — and conversely, `lang` itself (which only
    reflects automation's primary locale) must play no part in the comparison."""
    a = DeviceInfo(device_id="d1", lang="en-US", langs=frozenset({"en"}))
    b = DeviceInfo(device_id="d2", lang="en-US", langs=frozenset({"en", "fr"}))
    assert a.fingerprint != b.fingerprint

    c = DeviceInfo(device_id="d3", lang="fr-FR", langs=frozenset({"en"}))
    assert a.fingerprint == c.fingerprint


def test_pulled_file_as_dict_roundtrips_through_json(tmp_path):
    pf = PulledFile(
        kind=FileKind.BUNDLE,
        name="a.apks",
        local_path=tmp_path / "a.apks",
        size_bytes=10,
    )
    assert json.loads(json.dumps(pf.as_dict()))["kind"] == "bundle"


def test_device_outcome_ok_true_for_success_statuses():
    for status in (
        Status.INSTALLED,
        Status.UPDATED,
        Status.ALREADY_UP_TO_DATE,
        Status.SKIPPED_UPDATE_CHECK,
    ):
        assert make_outcome(status).ok is True


def test_device_outcome_ok_false_for_failure_statuses():
    for status in (Status.ERROR, Status.UNSUPPORTED_LOCALE):
        assert make_outcome(status).ok is False


def test_device_outcome_as_dict_is_json_serializable():
    outcome = make_outcome(Status.INSTALLED)
    json.dumps(outcome.as_dict())  # must not raise


def test_run_summary_exit_code_zero_when_all_succeed():
    summary = RunSummary(
        outcomes=[make_outcome(Status.INSTALLED), make_outcome(Status.UPDATED)]
    )
    assert summary.exit_code == 0
    assert summary.successful == 2
    assert summary.total == 2


def test_run_summary_exit_code_counts_failures():
    summary = RunSummary(
        outcomes=[make_outcome(Status.INSTALLED), make_outcome(Status.ERROR)]
    )
    assert summary.exit_code == 1


def test_run_summary_exit_code_caps_at_nine():
    summary = RunSummary(outcomes=[make_outcome(Status.ERROR) for _ in range(20)])
    assert summary.exit_code == 9


def test_run_summary_exit_code_fifty_when_no_devices():
    assert RunSummary(outcomes=[]).exit_code == 50


def test_run_summary_as_dict_is_json_serializable():
    summary = RunSummary(outcomes=[make_outcome(Status.INSTALLED)])
    payload = json.dumps(summary.as_dict())
    assert json.loads(payload)["total"] == 1
