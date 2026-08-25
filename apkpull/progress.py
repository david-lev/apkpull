"""Optional progress-reporting hook threaded through the pull pipeline.

Kept as a single narrow callback type so orchestrator.py/puller.py don't need
to know anything about *how* (or whether) progress gets rendered —
:mod:`apkpull.tui` is the only module that turns these events into an actual
live terminal display. Every call site guards with ``if report:`` so the
hook costs nothing when unused (the default library/CLI path).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class Stage(str, Enum):
    """One (device, package) pair's position in the pull pipeline. Ordered
    roughly as they occur, though DOWNLOADING can be skipped entirely (already
    up to date / ``skip_update_check``) and PACKAGING/VERIFYING/UNINSTALLING
    are each skippable too depending on flags and whether a bundle already
    exists locally.

    CONNECTING and LOCKED are device-scoped, not package-scoped — the same
    event fires for every package on that device at once (a locked or
    still-connecting device blocks all of them together), via
    ``run_for_device``'s ``broadcast`` closure rather than the per-package
    ``report`` one.

    MERGING sits between PULLING and PACKAGING: apkpull waits for every
    targeted device to finish a package before merging their splits into one
    bundle (see ``orchestrator._merge_pending_contributions``), so a device
    fires MERGING the moment its own raw pull finishes -- otherwise it would
    look stalled while waiting on slower sibling devices in its
    (package, version_code) group. PACKAGING/VERIFYING and the final
    DONE/ERROR for such a device only fire once its whole group resolves,
    which can be a real wall-clock gap after MERGING -- that's expected, not
    a stall.
    """

    CONNECTING = "connecting"
    LOCKED = "locked"
    QUEUED = "queued"
    OPENING_PLAY_STORE = "opening_play_store"
    DOWNLOADING = "downloading"
    """``detail`` distinguishes what's actually happening: ``"install"`` or
    ``"update"``, with a ``"-retry"`` suffix after a download-timeout retry
    (e.g. ``"update-retry"``) -- see PackageReporter call sites."""
    PULLING = "pulling"
    MERGING = "merging"
    PACKAGING = "packaging"
    VERIFYING = "verifying"
    UNINSTALLING = "uninstalling"
    DONE = "done"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    device_id: str
    device_label: str
    package: str
    stage: Stage
    detail: str = ""
    device_meta: str = ""
    """E.g. ``"arm64-v8a, en-US"`` — empty until the device's info() resolves
    (the very first QUEUED events, fired before any device is even connected
    to, don't have it yet)."""
    path: str = ""
    """Set only on DONE — where the pulled file(s)/folder ended up locally."""


ProgressCallback = Callable[[ProgressEvent], None]
"""Signature for the top-level hook passed to :func:`apkpull.orchestrator.run`."""


class PackageReporter(Protocol):
    """Signature for the per-device closure (``package, stage, detail``,
    optionally ``path``) threaded into :func:`apkpull.orchestrator._pull_and_finish`
    and :meth:`apkpull.puller.Puller.pull_raw` — device identity is already
    bound by the closure, so call sites only need to name the package.
    ``path`` is only ever passed (as a keyword) on the DONE call site — a
    :class:`typing.Protocol` rather than a plain ``Callable[...]`` alias
    specifically so that optional/keyword parameter is actually represented
    in the type, not just documented here."""

    def __call__(
        self, package: str, stage: Stage, detail: str = "", path: str = ""
    ) -> None: ...


DeviceReporter = Callable[[Stage, str], None]
"""Signature for a device-scoped closure (``stage, detail``, no package) —
threaded into :meth:`apkpull.automation.PlayStoreAutomator.wait_for_unlock`,
which blocks the whole device, not any one package."""
