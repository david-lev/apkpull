import json
from unittest.mock import patch

from apkpull.cli import build_parser, main
from apkpull.exceptions import NoDevicesFoundError
from apkpull.models import DeviceInfo, DeviceOutcome, OutputFormat, RunSummary, Status


def make_summary(*statuses: Status) -> RunSummary:
    return RunSummary(
        outcomes=[
            DeviceOutcome(
                device=DeviceInfo(device_id=f"d{i}"), package="com.app", status=s
            )
            for i, s in enumerate(statuses)
        ]
    )


def test_parser_requires_packages():
    parser = build_parser()
    args = parser.parse_args(["com.whatsapp"])
    assert args.packages == "com.whatsapp"
    assert args.uninstall_after is False
    assert args.verbose == 0
    assert args.format == OutputFormat.APKS.value
    assert args.unlock_timeout == 300.0
    assert args.keep_screen_on is True
    assert args.download_timeout == 300.0
    assert args.download_retries == 1
    assert args.skip_duplicate_check is False
    assert args.skip_update_check is False
    assert args.no_live is False
    assert args.full_manifest is False


def test_parser_accepts_valid_format_choices():
    parser = build_parser()
    for fmt in ("apks", "zip", "folder"):
        assert parser.parse_args(["com.app", "--format", fmt]).format == fmt


def test_parser_rejects_invalid_format(capsys):
    parser = build_parser()
    try:
        parser.parse_args(["com.app", "--format", "rar"])
        raised = False
    except SystemExit:
        raised = True
    assert raised


def test_parser_parses_all_flags():
    parser = build_parser()
    args = parser.parse_args(
        [
            "com.whatsapp",
            "-d",
            "/tmp/out",
            "--uninstall-after",
            "--devices",
            "a,b",
            "--max-workers",
            "2",
            "--max-poll-rounds",
            "9",
            "--unlock-timeout",
            "60",
            "--download-timeout",
            "120",
            "--download-retries",
            "3",
            "--format",
            "zip",
            "--notify",
            "--no-keep-screen-on",
            "--skip-duplicate-check",
            "--skip-update-check",
            "--no-live",
            "--no-verify",
            "--strict-verify",
            "--full-manifest",
            "--json",
            "-vv",
        ]
    )
    assert str(args.dest) == "/tmp/out"
    assert args.uninstall_after is True
    assert args.devices == "a,b"
    assert args.max_workers == 2
    assert args.max_poll_rounds == 9
    assert args.unlock_timeout == 60.0
    assert args.download_timeout == 120.0
    assert args.download_retries == 3
    assert args.format == "zip"
    assert args.notify is True
    assert args.keep_screen_on is False
    assert args.skip_duplicate_check is True
    assert args.skip_update_check is True
    assert args.no_live is True
    assert args.no_verify is True
    assert args.strict_verify is True
    assert args.full_manifest is True
    assert args.json is True
    assert args.verbose == 2


def test_main_returns_summary_exit_code():
    with patch("apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)):
        assert main(["com.app"]) == 0


def test_main_returns_nonzero_on_partial_failure():
    with patch(
        "apkpull.cli.run_pull",
        return_value=make_summary(Status.INSTALLED, Status.ERROR),
    ):
        assert main(["com.app"]) == 1


def test_main_prints_json_summary(capsys):
    with patch("apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)):
        main(["com.app", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["total"] == 1
    assert payload["successful"] == 1


def test_main_handles_apkpull_error_gracefully(capsys):
    with patch("apkpull.cli.run_pull", side_effect=NoDevicesFoundError("no devices")):
        code = main(["com.app"])
    assert code == 1


def test_main_json_error_output(capsys):
    with patch("apkpull.cli.run_pull", side_effect=NoDevicesFoundError("no devices")):
        main(["com.app", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "no devices" in payload["error"]


def test_main_returns_130_on_keyboard_interrupt(capsys):
    """130 (128 + SIGINT) is the standard shell convention for a Ctrl+C exit,
    and main() must catch it explicitly -- otherwise the user gets a raw
    traceback instead of a clean message."""
    with patch("apkpull.cli.run_pull", side_effect=KeyboardInterrupt):
        code = main(["com.app"])
    assert code == 130


def test_main_json_output_on_keyboard_interrupt(capsys):
    with patch("apkpull.cli.run_pull", side_effect=KeyboardInterrupt):
        main(["com.app", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert "Interrupted" in payload["error"]


def test_main_passes_parsed_args_through_to_run_pull():
    with patch(
        "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
    ) as run_mock:
        main(
            [
                "com.app",
                "--devices",
                "a,b",
                "--uninstall-after",
                "--strict-verify",
                "--full-manifest",
            ]
        )
    _, kwargs = run_mock.call_args
    assert run_mock.call_args.args[0] == ["com.app"]
    assert kwargs["device_ids"] == ["a", "b"]
    assert kwargs["uninstall"] is True
    assert kwargs["strict_verify"] is True
    assert kwargs["full_manifest"] is True
    assert kwargs["output_format"] == OutputFormat.APKS


def test_main_uses_live_display_on_a_real_terminal_by_default():
    with (
        patch(
            "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
        ) as run_mock,
        patch("apkpull.cli.LiveDisplay") as display_ctor,
        patch("sys.stderr.isatty", return_value=True),
    ):
        main(["com.app"])
    display_ctor.assert_called_once()
    assert run_mock.call_args.kwargs["on_progress"] == display_ctor.return_value.update


def test_main_skips_live_display_with_no_live_flag():
    with (
        patch(
            "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
        ) as run_mock,
        patch("apkpull.cli.LiveDisplay") as display_ctor,
        patch("sys.stderr.isatty", return_value=True),
    ):
        main(["com.app", "--no-live"])
    display_ctor.assert_not_called()
    assert run_mock.call_args.kwargs["on_progress"] is None


def test_main_still_uses_live_display_when_verbose():
    """-v/-vv logs print through the same console as any other log line and
    scroll in above the live table (see tui.py's module docstring) -- no
    need to force plain logs just because verbose logging is on; --no-live
    is the explicit way to skip the table."""
    with (
        patch(
            "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
        ) as run_mock,
        patch("apkpull.cli.LiveDisplay") as display_ctor,
        patch("sys.stderr.isatty", return_value=True),
    ):
        main(["com.app", "-v"])
    display_ctor.assert_called_once()
    assert run_mock.call_args.kwargs["on_progress"] is not None


def test_main_skips_live_display_on_a_non_terminal_stderr():
    with (
        patch(
            "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
        ) as run_mock,
        patch("apkpull.cli.LiveDisplay") as display_ctor,
        patch("sys.stderr.isatty", return_value=False),
    ):
        main(["com.app"])
    display_ctor.assert_not_called()
    assert run_mock.call_args.kwargs["on_progress"] is None


def test_main_splits_comma_separated_packages():
    with patch(
        "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
    ) as run_mock:
        main(["com.whatsapp, com.spotify.music"])
    assert run_mock.call_args.args[0] == ["com.whatsapp", "com.spotify.music"]


def test_main_passes_format_and_unlock_timeout_through_to_run_pull():
    with patch(
        "apkpull.cli.run_pull", return_value=make_summary(Status.INSTALLED)
    ) as run_mock:
        main(["com.app", "--format", "folder", "--unlock-timeout", "45"])
    _, kwargs = run_mock.call_args
    assert kwargs["output_format"] == OutputFormat.FOLDER
    assert kwargs["automation_config"].unlock_timeout == 45.0
