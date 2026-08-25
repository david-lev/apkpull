"""Live terminal display: one table per package, devices as rows, updating in
place as :class:`~apkpull.progress.ProgressEvent`\\ s arrive.

Every device/package pair keeps its row for the whole run, even once it
hits ``DONE``/``ERROR`` -- the table is meant to be a stable, complete
record of every device's status, not something rows disappear from as work
finishes. ``logging`` warnings and (optionally, see ``apkpull.cli``) verbose
logs print through the same ``rich`` console and appear scrolled in above
the live region, which is where rich's ``Live`` places anything printed
while it's active -- as long as the live region itself stays within the
terminal's height. Once it doesn't, every refresh tick re-emits more lines
than fit, so the excess forces a real scroll each time, continuously
carrying earlier output (including those warnings) further up and
eventually out of scrollback -- a genuinely large run (many packages and/or
devices) can hit this. ``--no-live`` is the escape valve for that case: it
skips this table entirely in favor of plain, unbounded log lines, which
can't hit this problem since nothing is redrawn in place.

Deliberately kept separate from orchestrator.py/puller.py/automation.py —
they only know how to *emit* :class:`~apkpull.progress.ProgressEvent`\\ s via
an optional callback; this module is the only place that turns those into a
rendered display. :func:`apkpull.cli.main` decides whether to use it at all.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

from rich.cells import cell_len
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.table import Table
from rich.text import Text

from .models import Status
from .progress import ProgressEvent, Stage

# Stages with one fixed icon/label, no detail-dependent wording. DOWNLOADING
# and OPENING_PLAY_STORE are handled separately in _RowState.status_text —
# their wording depends on install-vs-update and retry status.
_STAGE_LABELS: dict[Stage, tuple[str, str]] = {
    Stage.CONNECTING: ("🔌", "Connecting…"),
    Stage.QUEUED: ("⏳", "Queued"),
    Stage.MERGING: ("🧩", "Waiting for other devices…"),
    Stage.PACKAGING: ("📦", "Packaging…"),
    Stage.VERIFYING: ("🔍", "Verifying…"),
    Stage.UNINSTALLING: ("🗑", "Uninstalling…"),
}

_DONE_LABELS: dict[str, str] = {
    # INSTALLED/UPDATED are real device-state changes that happened *this
    # run* -- worth calling out specifically. ALREADY_UP_TO_DATE and
    # SKIPPED_UPDATE_CHECK didn't change anything on the device; apkpull's
    # actual job there was just retrieving the files, so "Pulled" is the
    # accurate word, not "Installed" (nothing was (re)installed).
    Status.INSTALLED.value: "Installed",
    Status.UPDATED.value: "Updated",
    Status.ALREADY_UP_TO_DATE.value: "Pulled",
    Status.SKIPPED_UPDATE_CHECK.value: "Pulled",
}


def _format_elapsed(seconds: float) -> str:
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


@dataclass(slots=True)
class _RowState:
    device_label: str
    device_meta: str = ""
    stage: Stage = Stage.QUEUED
    detail: str = ""
    path: str = ""
    """Set once DONE -- the local destination the files were pulled to."""
    started_at: float | None = None
    """``None`` while still queued -- elapsed shows "—" until real work starts."""
    finished_at: float | None = None
    """Set once DONE/ERROR, so elapsed freezes instead of ticking past completion."""

    def device_text(self) -> str:
        text = f"📱 {self.device_label}"
        if self.device_meta:
            text += f"  ({self.device_meta})"
        return text

    def status_text(self) -> Text:
        # overflow/no_wrap are set on the Status column itself (see render()),
        # not here -- Rich ignores a cell's own Text settings in favor of its
        # column's, so setting them on these Text objects would do nothing.
        if self.stage == Stage.ERROR:
            return Text(f"❌ {self.detail or 'Error'}", style="red")
        if self.stage == Stage.DONE:
            label = _DONE_LABELS.get(self.detail, self.detail or "Done")
            if self.path:
                return Text(f"✅ {label} → {self.path}", style="green")
            return Text(f"✅ {label}", style="green")
        if self.stage == Stage.LOCKED:
            return Text("🔒 Locked, please unlock…", style="yellow")
        if self.stage == Stage.PULLING:
            suffix = f" {self.detail}" if self.detail else "…"
            return Text(f"📥 Pulling{suffix}")
        if self.stage == Stage.DOWNLOADING:
            kind = self.detail.removesuffix("-retry")
            is_retry = self.detail.endswith("-retry")
            icon, verb = (
                ("🔄", "Updating") if kind == "update" else ("⬇", "Downloading")
            )
            suffix = " (retry)" if is_retry else ""
            return Text(f"{icon} {verb}…{suffix}")
        if self.stage == Stage.OPENING_PLAY_STORE:
            suffix = " (retry)" if self.detail.endswith("-retry") else ""
            return Text(f"📲 Opening Play Store…{suffix}")
        icon, label = _STAGE_LABELS.get(self.stage, ("", self.stage.value))
        return Text(f"{icon} {label}")

    def elapsed_text(self) -> str:
        if self.started_at is None:
            return "—"
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return _format_elapsed(end - self.started_at)

    @property
    def is_terminal(self) -> bool:
        return self.stage in (Stage.DONE, Stage.ERROR)


class LiveDisplay:
    """Context manager: ``with LiveDisplay() as display: run(..., on_progress=display.update)``.

    ``update`` is safe to call from any thread — apkpull fans work out across
    devices on a thread pool, and every device's worker thread reports its
    own progress concurrently. rich.live.Live's own refresh loop (not this
    class) is what actually redraws the terminal, on a timer, so elapsed
    times tick smoothly even between events rather than only on new ones.
    """

    def __init__(self, console: Console | None = None) -> None:
        self.console = console or Console(stderr=True)
        self._rows: dict[tuple[str, str], _RowState] = {}
        self._package_order: list[str] = []
        self._lock = threading.Lock()
        self._live = Live(
            _Renderable(self),
            console=self.console,
            refresh_per_second=4,
            transient=False,
        )

    def __enter__(self) -> LiveDisplay:  # noqa: PYI034 - Self needs Python 3.11+
        self._live.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._live.stop()

    def update(self, event: ProgressEvent) -> None:
        key = (event.device_id, event.package)
        with self._lock:
            row = self._rows.get(key)
            if row is None:
                row = _RowState(device_label=event.device_label)
                self._rows[key] = row
                if event.package not in self._package_order:
                    self._package_order.append(event.package)
            row.device_label = event.device_label
            if event.device_meta:
                row.device_meta = event.device_meta
            if event.path:
                row.path = event.path
            if row.started_at is None and event.stage != Stage.QUEUED:
                row.started_at = time.monotonic()
            if event.stage in (Stage.DONE, Stage.ERROR):
                row.finished_at = time.monotonic()
            row.stage = event.stage
            row.detail = event.detail

    def render(self) -> RenderableType:
        with self._lock:
            packages = list(self._package_order)
            rows_by_package: dict[str, list[tuple[str, _RowState]]] = {}
            for (device_id, package), row in self._rows.items():
                rows_by_package.setdefault(package, []).append((device_id, row))
            total = len(self._rows)
            done = sum(1 for row in self._rows.values() if row.is_terminal)
            failed = sum(1 for row in self._rows.values() if row.stage == Stage.ERROR)

            # Fixed, shared widths for every table this frame -- otherwise each
            # table auto-sizes its Device/Elapsed columns from only its own
            # rows, so column boundaries drift table to table (and frame to
            # frame, as elapsed digits/labels change length) instead of lining
            # up into a steady grid.
            device_col_width = max(
                (cell_len(row.device_text()) for row in self._rows.values()),
                default=0,
            )
            device_col_width = max(device_col_width, cell_len("Device"))
            elapsed_col_width = max(
                (len(row.elapsed_text()) for row in self._rows.values()),
                default=0,
            )
            elapsed_col_width = max(elapsed_col_width, len("Elapsed"))

        tables: list[RenderableType] = []
        for package in packages:
            device_rows = rows_by_package.get(package, [])
            table = Table(title=package, title_justify="left", expand=True)
            table.add_column("Device", width=device_col_width, no_wrap=True)
            # no_wrap/overflow must be set on the *column*, not on the Text
            # renderables in each cell -- Table._render always uses the
            # column's settings for every cell regardless of what a Text
            # object itself asks for. Without this, a long unbroken value
            # (e.g. a filesystem path with no spaces to wrap on) forces the
            # column wider than its ratio share to fit it, which is what
            # was throwing the whole table's column boundaries out of line.
            table.add_column("Status", ratio=1, no_wrap=True, overflow="ellipsis")
            table.add_column(
                "Elapsed", justify="right", width=elapsed_col_width, no_wrap=True
            )
            for _device_id, row in device_rows:
                table.add_row(row.device_text(), row.status_text(), row.elapsed_text())
            tables.append(table)

        summary = f"{done}/{total} complete" + (f" · {failed} failed" if failed else "")
        return Group(*tables, Text(summary, style="bold"))


class _Renderable:
    """Thin adapter so rich re-renders fresh (recomputed elapsed times, latest
    state) on every refresh tick, not just when :meth:`LiveDisplay.update` is
    called -- ``Live`` re-invokes ``__rich_console__`` on its own timer."""

    def __init__(self, display: LiveDisplay) -> None:
        self._display = display

    def __rich_console__(self, console, options):
        yield self._display.render()
