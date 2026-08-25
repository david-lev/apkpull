from unittest.mock import patch

from rich.cells import cell_len
from rich.console import Console
from rich.table import Table

from apkpull.models import Status
from apkpull.progress import ProgressEvent, Stage
from apkpull.tui import LiveDisplay


def make_display() -> tuple[LiveDisplay, Console]:
    console = Console(record=True, force_terminal=True, width=100)
    return LiveDisplay(console=console), console


def render_text(display: LiveDisplay) -> str:
    display.console.print(display.render())
    return display.console.export_text()


def test_queued_row_shows_dash_for_elapsed():
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "dev-1", "com.app", Stage.QUEUED))
    text = render_text(display)
    assert "Queued" in text
    assert "—" in text


def test_package_table_title_shows_package_name_and_rows_show_device():
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "dev-1", "com.app", Stage.QUEUED))
    text = render_text(display)
    assert "com.app" in text
    assert "arm64-v8a" not in text  # not known yet

    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.app",
            Stage.DOWNLOADING,
            device_meta="arm64-v8a, en-US",
        )
    )
    text = render_text(display)
    assert "Pixel 7" in text
    assert "arm64-v8a, en-US" in text


def test_devices_group_under_their_own_package_table():
    display, _console = make_display()
    for device_id, label in (("dev-1", "Pixel 7"), ("dev-2", "sdk_gphone64_arm64")):
        for package in ("com.whatsapp", "com.spotify.music"):
            display.update(ProgressEvent(device_id, label, package, Stage.QUEUED))
    text = render_text(display)
    assert text.count("Device") == 2  # one table header per package
    assert "com.whatsapp" in text
    assert "com.spotify.music" in text
    assert "Pixel 7" in text
    assert "sdk_gphone64_arm64" in text


def test_device_and_elapsed_columns_share_width_across_tables():
    """Each package gets its own table, but a short device label in one
    table must not leave its Device column narrower than a long label's in
    another -- otherwise the tables' column boundaries drift out of line
    with each other."""
    display, _console = make_display()
    display.update(
        ProgressEvent("dev-1", "sdk_gphone64_arm64", "com.tranzmate", Stage.QUEUED)
    )
    display.update(ProgressEvent("dev-2", "P", "com.other", Stage.QUEUED))
    group = display.render()
    tables = [r for r in group.renderables if isinstance(r, Table)]
    assert len(tables) == 2
    device_widths = {t.columns[0].width for t in tables}
    elapsed_widths = {t.columns[2].width for t in tables}
    assert len(device_widths) == 1
    assert len(elapsed_widths) == 1
    assert device_widths.pop() == max(
        cell_len("📱 sdk_gphone64_arm64"), cell_len("Device")
    )


def test_status_column_is_configured_for_single_line_truncation():
    """Regression test: rich.table.Table._render always uses the *column's*
    no_wrap/overflow settings for every cell, ignoring whatever a Text object
    in that cell asks for -- so a long unbroken value (e.g. a filesystem path,
    which has no spaces to wrap on) must be constrained at the column level,
    or it forces the column wider than its ratio share and throws every
    table's column boundaries out of alignment with each other."""
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "Pixel 7", "com.app", Stage.QUEUED))
    group = display.render()
    tables = [r for r in group.renderables if isinstance(r, Table)]
    status_column = tables[0].columns[1]
    assert status_column.no_wrap is True
    assert status_column.overflow == "ellipsis"


def test_connecting_stage_is_shown():
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "Pixel 7", "com.app", Stage.CONNECTING))
    text = render_text(display)
    assert "Connecting" in text


def test_locked_stage_prompts_to_unlock():
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "Pixel 7", "com.app", Stage.LOCKED))
    text = render_text(display)
    assert "Locked" in text
    assert "unlock" in text


def test_downloading_stage_distinguishes_install_from_update():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.app", Stage.DOWNLOADING, detail="install"
        )
    )
    text = render_text(display)
    assert "Downloading" in text
    assert "Updating" not in text

    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.other", Stage.DOWNLOADING, detail="update"
        )
    )
    text = render_text(display)
    assert "Updating" in text


def test_downloading_retry_shows_retry_suffix():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.app", Stage.DOWNLOADING, detail="update-retry"
        )
    )
    text = render_text(display)
    assert "Updating" in text
    assert "retry" in text


def test_done_stage_shows_installed_or_updated_when_it_actually_happened():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.app", Stage.DONE, detail=Status.INSTALLED.value
        )
    )
    text = render_text(display)
    assert "Installed" in text
    assert "✅" in text

    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.other", Stage.DONE, detail=Status.UPDATED.value
        )
    )
    text = render_text(display)
    assert "Updated" in text


def test_done_stage_shows_pulled_when_nothing_was_installed():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.app",
            Stage.DONE,
            detail=Status.ALREADY_UP_TO_DATE.value,
        )
    )
    text = render_text(display)
    assert "Pulled" in text
    assert "Installed" not in text

    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.other",
            Stage.DONE,
            detail=Status.SKIPPED_UPDATE_CHECK.value,
        )
    )
    text = render_text(display)
    assert "Pulled" in text


def test_done_stage_always_shows_the_destination_path():
    """apkpull pulls files on every successful status, not just the ones
    labeled "Pulled" -- INSTALLED/UPDATED also pulled files, they just *also*
    changed something on the device. The destination should show regardless
    of which of the two happened."""
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.app",
            Stage.DONE,
            detail=Status.ALREADY_UP_TO_DATE.value,
            path="/tmp/out/com.app.apks",
        )
    )
    text = render_text(display)
    assert "Pulled" in text
    assert "/tmp/out/com.app.apks" in text

    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.other",
            Stage.DONE,
            detail=Status.INSTALLED.value,
            path="/tmp/out/com.other.apks",
        )
    )
    text = render_text(display)
    assert "Installed" in text
    assert "/tmp/out/com.other.apks" in text


def test_error_stage_shows_the_error_message():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.app", Stage.ERROR, detail="Something broke"
        )
    )
    text = render_text(display)
    assert "Something broke" in text
    assert "❌" in text


def test_pulling_stage_shows_the_file_detail():
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1",
            "Pixel 7",
            "com.app",
            Stage.PULLING,
            detail="config.xxhdpi.apk (8.0M)",
        )
    )
    text = render_text(display)
    assert "config.xxhdpi.apk (8.0M)" in text


def test_rows_stay_in_the_table_after_reaching_a_terminal_stage():
    """The table is meant to be a stable, complete record of every device's
    status for the whole run -- DONE/ERROR rows must keep their spot instead
    of disappearing, so the operator can still see what happened to every
    device/package at a glance once things finish."""
    display, _console = make_display()
    display.update(
        ProgressEvent(
            "dev-1", "Pixel 7", "com.app", Stage.DONE, detail=Status.INSTALLED.value
        )
    )
    display.update(
        ProgressEvent(
            "dev-2", "Pixel 8", "com.app", Stage.ERROR, detail="Something broke"
        )
    )
    assert ("dev-1", "com.app") in display._rows
    assert ("dev-2", "com.app") in display._rows
    text = render_text(display)
    assert "Pixel 7" in text
    assert "Pixel 8" in text
    assert "Installed" in text
    assert "Something broke" in text


def test_summary_counts_only_terminal_stages_as_complete():
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "dev-1", "com.app", Stage.QUEUED))
    display.update(ProgressEvent("dev-1", "dev-1", "com.other", Stage.DOWNLOADING))
    display.update(
        ProgressEvent(
            "dev-1", "dev-1", "com.done", Stage.DONE, detail=Status.INSTALLED.value
        )
    )
    display.update(
        ProgressEvent("dev-1", "dev-1", "com.failed", Stage.ERROR, detail="boom")
    )
    text = render_text(display)
    assert "2/4 complete" in text
    assert "1 failed" in text


def test_elapsed_freezes_once_a_row_reaches_a_terminal_stage():
    """elapsed_text() must not keep growing as real wall-clock time passes
    once a row is DONE/ERROR -- it should use the frozen finished_at instead
    of calling time.monotonic() again."""
    display, _console = make_display()
    display.update(ProgressEvent("dev-1", "dev-1", "com.app", Stage.DOWNLOADING))
    row = display._rows[("dev-1", "com.app")]
    row.started_at -= 5  # simulate 5s of real elapsed time before completion
    display.update(
        ProgressEvent(
            "dev-1", "dev-1", "com.app", Stage.DONE, detail=Status.INSTALLED.value
        )
    )
    assert row.finished_at is not None

    first_read = row.elapsed_text()
    assert first_read == "0:05"
    with patch("apkpull.tui.time.monotonic", return_value=row.finished_at + 100):
        # Real wall-clock time has "passed" (time.monotonic() would now
        # return a much later value), but the row is DONE, so this must not
        # be reflected -- elapsed stays pinned to finished_at - started_at.
        second_read = row.elapsed_text()
    assert second_read == first_read


def test_live_display_is_a_working_context_manager():
    """Exercises the real rich.live.Live start/stop + background refresh
    thread, not just the pure render() logic above."""
    display, _console = make_display()
    with display:
        display.update(ProgressEvent("dev-1", "dev-1", "com.app", Stage.DOWNLOADING))
    # No exception/hang on __exit__, and the final state is still queryable.
    assert display._rows[("dev-1", "com.app")].stage == Stage.DOWNLOADING
