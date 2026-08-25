"""Command-line entry point.

apkpull com.whatsapp
apkpull com.whatsapp,com.spotify.music -d ~/Documents/my_apks --uninstall-after -vv
apkpull com.whatsapp --devices emulator-5554,emulator-5556 --json
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from contextlib import nullcontext
from pathlib import Path

from . import __version__
from .automation import AutomationConfig
from .exceptions import ApkPullError
from .locales import supported_languages
from .logging_setup import setup_logging
from .models import FileKind, OutputFormat
from .orchestrator import run as run_pull
from .tui import LiveDisplay

logger = logging.getLogger("apkpull.cli")

DEFAULT_DEST = Path.home() / "Downloads" / "APKpull"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="apkpull",
        description="Pull an Android app's apk/obb files from Google Play via one or more adb-connected devices.",
    )
    parser.add_argument(
        "packages",
        help="Package name, e.g. com.whatsapp. Comma-separated for more than "
        "one, e.g. com.whatsapp,com.spotify.music — apps on the same device "
        "download concurrently, same as tapping Install on each from Google Play.",
    )
    parser.add_argument(
        "-d",
        "--dest",
        type=Path,
        default=DEFAULT_DEST,
        metavar="DIR",
        help=f"Directory to pull files into (default: {DEFAULT_DEST})",
    )
    parser.add_argument(
        "--uninstall-after",
        action="store_true",
        help="Uninstall the app after pulling it.",
    )
    parser.add_argument(
        "--devices",
        metavar="ID1,ID2,...",
        help="Comma-separated device ids to target (default: every device adb sees).",
    )
    parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Max devices processed concurrently.",
    )
    parser.add_argument(
        "--max-poll-rounds",
        type=int,
        default=5,
        help="UI-polling rounds before giving up on a device.",
    )
    parser.add_argument(
        "--unlock-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for a locked device to unlock before giving up on it "
        "(0 = wait forever). Default: 300.",
    )
    parser.add_argument(
        "--download-timeout",
        type=float,
        default=300.0,
        help="Seconds to wait for a package's download/update to finish before "
        "retrying or giving up on it (0 = wait forever). Default: 300.",
    )
    parser.add_argument(
        "--download-retries",
        type=int,
        default=1,
        help="Times to restart a package's download/update after it times out, "
        "before reporting it as failed. Default: 1.",
    )
    parser.add_argument(
        "--format",
        choices=[f.value for f in OutputFormat],
        default=OutputFormat.APKS.value,
        help="Output format: 'apks' (SAI/bundletool zip, default), 'zip' (same contents, "
        ".zip extension), or 'folder' (extracted loose files).",
    )
    parser.add_argument(
        "--notify", action="store_true", help="Send native desktop notifications."
    )
    parser.add_argument(
        "--no-keep-screen-on",
        dest="keep_screen_on",
        action="store_false",
        help="Don't force the screen to stay awake while plugged in during the pull "
        "(the device may lock and interrupt automation).",
    )
    parser.add_argument(
        "--no-verify",
        action="store_true",
        help="Skip apkfile verification of pulled apks.",
    )
    parser.add_argument(
        "--strict-verify",
        action="store_true",
        help="Treat a verification mismatch as a device failure (implies verification is on).",
    )
    parser.add_argument(
        "--full-manifest",
        action="store_true",
        help="Write the full apkfile manifest (every permission's AOSP detail, exported/"
        "deep-link components, size breakdown, dex info, full certificate fields) to "
        "manifest.json instead of the trimmed default. No effect with --no-verify.",
    )
    parser.add_argument(
        "--skip-existence-check",
        action="store_true",
        help="Don't pre-check that the package exists on Google Play before touching devices.",
    )
    parser.add_argument(
        "--skip-duplicate-check",
        action="store_true",
        help="Don't warn about devices that look identical or share hardware but "
        "differ only in configured languages.",
    )
    parser.add_argument(
        "--skip-update-check",
        action="store_true",
        help="For a package already installed on a device, pull whatever's "
        "currently installed instead of checking Google Play for an update "
        "first — faster, and skips Play Store UI automation entirely for that "
        "package, but may pull a stale version.",
    )
    parser.add_argument(
        "--force-locale",
        choices=supported_languages(),
        default=None,
        help="Force Google Play to this language for the run, on every targeted "
        "device, reverting it back to whatever it was afterward — useful when a "
        "device's own language isn't one apkpull supports. Requires Android 13+ "
        "(API 33+) per-app locale overrides; on older devices this is skipped "
        "with a warning and that device falls back to its own language as usual.",
    )
    parser.add_argument(
        "--no-live",
        action="store_true",
        help="Don't show the live per-device/per-package status table — just plain, "
        "unbounded log lines. Useful for a run with enough devices/packages that the "
        "table would no longer fit the terminal (see the module docstring in tui.py "
        "for why that matters). Live display is also automatically skipped when "
        "stderr isn't a real terminal.",
    )
    parser.add_argument(
        "--adb-path",
        default=None,
        help="Path to the adb executable (default: search PATH).",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Print the run summary as JSON instead of text.",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=0,
        help="-v for info logs, -vv for debug logs.",
    )
    parser.add_argument("--version", action="version", version=f"apkpull {__version__}")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    device_ids = [d.strip() for d in args.devices.split(",")] if args.devices else None
    packages = [p.strip() for p in args.packages.split(",")]
    automation_config = AutomationConfig(
        max_rounds=args.max_poll_rounds, unlock_timeout=args.unlock_timeout
    )

    # -v/-vv's logs print through the same console and scroll in above the
    # live region same as any other log line (see tui.py's module
    # docstring) -- no need to force --no-live for them. Live display is
    # still auto-skipped for a non-terminal stderr (piped/redirected/CI):
    # there's nothing to redraw in place there, and rich would just print
    # one frame per line.
    use_live = not args.no_live and sys.stderr.isatty()
    display = LiveDisplay() if use_live else None
    # Route logging through the live display's own console when active, so
    # warnings/errors print correctly above its live region instead of
    # corrupting it (a plain direct stderr write would).
    setup_logging(args.verbose, console=display.console if display else None)

    try:
        with display if display is not None else nullcontext():
            summary = run_pull(
                packages,
                args.dest,
                device_ids=device_ids,
                uninstall=args.uninstall_after,
                max_workers=args.max_workers,
                notify_enabled=args.notify,
                verify=not args.no_verify,
                strict_verify=args.strict_verify,
                full_manifest=args.full_manifest,
                output_format=OutputFormat(args.format),
                adb_path=args.adb_path,
                automation_config=automation_config,
                skip_existence_check=args.skip_existence_check,
                keep_screen_on=args.keep_screen_on,
                download_timeout=args.download_timeout,
                download_retries=args.download_retries,
                skip_duplicate_check=args.skip_duplicate_check,
                skip_update_check=args.skip_update_check,
                force_locale=args.force_locale,
                on_progress=display.update if display else None,
            )
    except ApkPullError as exc:
        logger.error(str(exc))
        if args.json:
            print(json.dumps({"error": str(exc)}))
        return 1
    except KeyboardInterrupt:
        logger.warning("Interrupted by user.")
        if args.json:
            print(json.dumps({"error": "Interrupted by user (Ctrl+C)."}))
        return 130  # 128 + SIGINT — the standard shell convention

    if args.json:
        print(json.dumps(summary.as_dict(), indent=2))
    else:
        _print_summary(summary)

    return summary.exit_code


def _print_summary(summary) -> None:
    for outcome in summary.outcomes:
        icon = "OK " if outcome.ok else "FAIL"
        print(
            f"[{icon}] {outcome.device.label} / {outcome.package}: {outcome.status.value}"
            + (f" - {outcome.error}" if outcome.error else "")
        )
        for pulled in outcome.pulled_files:
            if pulled.kind == FileKind.OBB:
                size_mb = pulled.size_bytes / 1024 / 1024
                print(f"     OBB: {pulled.name} ({size_mb:.1f} MB)")
    print(f"\n{summary.successful}/{summary.total} successful operations!")


def run() -> None:  # console_script entry point (pyproject: apkpull = apkpull.cli:run)
    sys.exit(main())


if __name__ == "__main__":
    sys.exit(main())
