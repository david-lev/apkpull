"""Top-level entry points: validate packages, then run them across N devices concurrently.

This is the only module that knows how to fan work out across devices — adb,
device, automation and puller are all single-device/synchronous and get
composed here with a :class:`~concurrent.futures.ThreadPoolExecutor` (adb
round-trips are I/O bound, so threads are enough; no device touches another
device's state).

Within one device, multiple packages are handled in two phases rather than
threaded against each other, since a device has exactly one screen: a
"kickoff" pass visits each package's Play Store page just long enough to
start its install/update, then a completion pass polls every in-flight
package purely over adb (``pm path``/``dumpsys package``, no UI involved) and
pulls each as it resolves — which is what lets Google Play's own overlapping
downloads actually pay off, instead of apkpull serializing what Play Store
itself doesn't.
"""

from __future__ import annotations

import logging
import re
import shutil
import time
import urllib.error
import urllib.request
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path

from apkfile.install import uninstall_apks

from .adb import AdbClient
from .automation import AutomationConfig, PlayStoreAutomator
from .device import MIN_SDK_FOR_APP_LOCALES, Device
from .exceptions import (
    DeviceError,
    DownloadTimeoutError,
    GooglePlayUnavailableError,
    InvalidPackageNameError,
    NoDevicesFoundError,
    UnsupportedLocaleError,
)
from .locales import get_locale, supported_languages
from .models import (
    DeviceInfo,
    DeviceOutcome,
    FileKind,
    OutputFormat,
    PulledFile,
    RunSummary,
    Status,
)
from .notify import notify
from .progress import PackageReporter, ProgressCallback, ProgressEvent, Stage
from .puller import (
    Puller,
    RawPull,
    build_merged_bundle,
    group_staging_dir,
    size_of,
    staging_root,
)

logger = logging.getLogger("apkpull.orchestrator")

_PACKAGE_NAME_RE = re.compile(r"^([A-Za-z][A-Za-z0-9_]*\.)+[A-Za-z][A-Za-z0-9_]*$")
_PLAY_STORE_APP_PACKAGE = "com.android.vending"


@dataclass(slots=True)
class _Tracked:
    """A package whose install/update has been kicked off and is being polled."""

    kind: str
    """``"install"`` or ``"update"``."""
    baseline_version_code: int | None = None
    """For updates: the version code before the update, so a change means done."""
    started_at: float = 0.0
    """``time.monotonic()`` when this attempt was kicked off, for ``download_timeout``."""
    retries_left: int = 0


@dataclass(slots=True)
class _PendingContribution:
    """One device's successful raw pull, waiting for every other device
    targeting the same package to resolve (success or failure) before
    :func:`_merge_pending_contributions` builds the shared bundle once and
    patches ``outcome`` in place with the real ``pulled_files``/``status``.

    ``outcome`` is the *exact* :class:`~apkpull.models.DeviceOutcome`
    instance already appended to this device's outcome list — mutating it
    here is what lets the merge phase finish an outcome that
    ``_pull_and_finish`` had to leave incomplete, without constructing a
    second, competing one.
    """

    raw: RawPull
    outcome: DeviceOutcome


def validate_package_name(package: str) -> None:
    if not _PACKAGE_NAME_RE.match(package):
        raise InvalidPackageNameError(f"Invalid syntax for package name: {package!r}")


def check_package_exists(package: str, *, timeout: float = 3.0) -> None:
    """Best-effort, advisory-only check that ``package`` exists on the Play Store.

    Never raises — only logs. A 404 here means "the unauthenticated Play Store
    *website*, as seen from wherever apkpull's host happens to be, has no
    listing for this package at this address" — which is not the same question
    as "can the *device*'s Play Store account install it". A region-locked app
    (most banking apps, for instance) can easily 404 the web listing from one
    network/country while still installing fine on a device signed in with the
    right region/account, so a confirmed 404 here used to hard-abort the whole
    run before touching any device — this is the exact false-negative that
    forced that call to be removed. The automation flow's on-device checks are
    the actual source of truth for a package that truly doesn't exist.
    """
    url = f"https://play.google.com/store/apps/details?id={package}"
    request = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if response.status == 404:
                logger.warning(
                    "%s returned 404 from the Play Store website; it may not "
                    "exist, or may just be unavailable from this network/region. "
                    "Continuing anyway — device-side automation will surface a "
                    "clearer error if it truly can't be found.",
                    package,
                )
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            logger.warning(
                "%s returned 404 from the Play Store website; it may not "
                "exist, or may just be unavailable from this network/region. "
                "Continuing anyway — device-side automation will surface a "
                "clearer error if it truly can't be found.",
                package,
            )
    except urllib.error.URLError:
        logger.debug(
            "Could not reach Google Play to verify %s exists; continuing anyway.",
            package,
        )


def _log_and_notify_finish(
    label: str,
    package: str,
    destination: Path,
    pulled_files: list[PulledFile],
    *,
    status: Status,
    notify_enabled: bool,
) -> None:
    pulled_count = sum(1 for f in pulled_files if not f.already_existed)
    if pulled_count:
        logger.info(
            "[%s] %s: %d file(s) pulled to %s",
            label,
            package,
            pulled_count,
            destination,
        )
    else:
        logger.info(
            "[%s] %s: files already existed at %s, nothing new pulled.",
            label,
            package,
            destination,
        )
    if notify_enabled:
        notify(label, f"{package}: {status.value} ({pulled_count} file(s) pulled)")


def _pull_and_finish(
    adb: AdbClient,
    device: Device,
    info: DeviceInfo,
    package: str,
    status: Status,
    dest_root: Path,
    *,
    uninstall: bool,
    notify_enabled: bool,
    output_format: OutputFormat,
    start: float,
    report: PackageReporter | None = None,
) -> tuple[DeviceOutcome, _PendingContribution | None]:
    puller = Puller(device, dest_root)
    raw = puller.pull_raw(package, output_format=output_format, report=report)

    uninstalled = False
    if uninstall:
        # Safe immediately after the raw pull regardless of whether this
        # device's bundle needs merging with others -- its files are
        # already off the device by this point, merging is purely local
        # file work from here on.
        logger.info("[%s] Uninstalling %s...", info.label, package)
        if report:
            report(package, Stage.UNINSTALLING, "")
        uninstall_apks(package, device_id=device.device_id, adb_path=adb.adb_path)
        uninstalled = True

    if raw.already_built:
        pulled_files = [
            PulledFile(
                kind=FileKind.BUNDLE,
                name=raw.target.name,
                local_path=raw.target,
                size_bytes=size_of(raw.target),
                already_existed=True,
            ),
            *raw.obb_pulled_files,
        ]
        outcome = DeviceOutcome(
            device=info,
            package=package,
            status=status,
            pulled_files=pulled_files,
            version_code=raw.version_code,
            version_name=raw.version_name,
            destination=raw.target,
            duration_s=time.monotonic() - start,
            uninstalled=uninstalled,
        )
        if report:
            report(package, Stage.DONE, status.value, path=str(raw.target))
        _log_and_notify_finish(
            info.label,
            package,
            raw.target,
            pulled_files,
            status=status,
            notify_enabled=notify_enabled,
        )
        return outcome, None

    # This device's splits might not be the whole picture -- other devices
    # targeting this same package could still be mid-flight. `destination`
    # is knowable now (target_path is a pure function of package/version/
    # format), but `pulled_files` genuinely isn't until every device in this
    # (package, version_code) group has resolved and _merge_pending_contributions
    # builds the shared bundle once -- see that function for what happens next.
    if report:
        report(package, Stage.MERGING, "")
    outcome = DeviceOutcome(
        device=info,
        package=package,
        status=status,
        pulled_files=[],
        version_code=raw.version_code,
        version_name=raw.version_name,
        destination=raw.target,
        duration_s=time.monotonic() - start,
        uninstalled=uninstalled,
    )
    return outcome, _PendingContribution(raw=raw, outcome=outcome)


def run_for_device(
    adb: AdbClient,
    device_id: str,
    packages: list[str],
    dest_root: Path,
    *,
    uninstall: bool = False,
    notify_enabled: bool = False,
    output_format: OutputFormat = OutputFormat.APKS,
    automation_config: AutomationConfig | None = None,
    keep_screen_on: bool = True,
    download_timeout: float = 300.0,
    download_retries: int = 1,
    skip_update_check: bool = False,
    force_locale: str | None = None,
    on_progress: ProgressCallback | None = None,
    device_info: DeviceInfo | None = None,
) -> tuple[list[DeviceOutcome], list[_PendingContribution]]:
    device = Device(adb, device_id)
    if device_info is not None:
        # Already resolved once by _warn_about_duplicate_devices, right before
        # the thread pool was created -- reuse it instead of re-issuing the
        # same getprop/locale round trip a second time for this device.
        device.seed_info(device_info)
    start = time.monotonic()
    stay_on_previous: str | None = None
    locale_override_active = False
    previous_play_locale = ""
    outcomes: list[DeviceOutcome] = []
    pending: list[_PendingContribution] = []

    def broadcast(label: str, stage: Stage, detail: str = "") -> None:
        """For device-scoped states (connecting, locked) that block every
        package on this device at once, not just one — fires the same event
        for each of them."""
        if on_progress:
            for pkg in packages:
                on_progress(ProgressEvent(device_id, label, pkg, stage, detail))

    def error_outcome(
        pkg: str, exc: Exception, device_info: DeviceInfo
    ) -> DeviceOutcome:
        logger.error("[%s] %s: %s", device_info.label, pkg, exc)
        if notify_enabled:
            notify(device_info.label, f"{pkg}: {exc}")
        if on_progress:
            on_progress(
                ProgressEvent(device_id, device_info.label, pkg, Stage.ERROR, str(exc))
            )
        return DeviceOutcome(
            device=device_info,
            package=pkg,
            status=Status.ERROR,
            error=str(exc),
            duration_s=time.monotonic() - start,
        )

    broadcast(device_id, Stage.CONNECTING)

    try:
        device.ensure_connected()
        info = device.info()
        logger.info(
            "[%s] Connected (%s, %s processor).",
            info.label,
            info.model,
            info.abi,
        )

        if not device.is_installed(_PLAY_STORE_APP_PACKAGE) or device.is_disabled(
            _PLAY_STORE_APP_PACKAGE
        ):
            raise GooglePlayUnavailableError(
                "Google Play is disabled or not installed."
            )

        buttons = get_locale(info.lang)
        if force_locale:
            if info.sdk is not None and info.sdk >= MIN_SDK_FOR_APP_LOCALES:
                previous_play_locale = device.get_app_locale(_PLAY_STORE_APP_PACKAGE)
                device.set_app_locale(_PLAY_STORE_APP_PACKAGE, force_locale)
                device.force_stop(_PLAY_STORE_APP_PACKAGE)
                locale_override_active = True
                buttons = get_locale(force_locale)
                logger.info(
                    "[%s] Forced Google Play to '%s' for this run (was %s).",
                    info.label,
                    force_locale,
                    previous_play_locale or "system default",
                )
            else:
                logger.warning(
                    "[%s] Can't force Google Play's locale: needs Android %d+ "
                    "(this device is API %s). Falling back to its own language.",
                    info.label,
                    MIN_SDK_FOR_APP_LOCALES,
                    info.sdk,
                )
        if buttons is None:
            raise UnsupportedLocaleError(
                f"Device language ({info.lang}) is not supported by apkpull; install/update manually."
            )

        automator = PlayStoreAutomator(device, buttons, automation_config)
        automator.wait_for_unlock(
            report=lambda stage, detail="": broadcast(info.label, stage, detail)
        )

        if keep_screen_on:
            stay_on_previous = device.get_setting("global", "stay_on_while_plugged_in")
            device.put_setting("global", "stay_on_while_plugged_in", "7")

        def report(
            package: str, stage: Stage, detail: str = "", path: str = ""
        ) -> None:
            if on_progress:
                on_progress(
                    ProgressEvent(
                        device_id,
                        info.label,
                        package,
                        stage,
                        detail,
                        f"{info.abi}, {info.lang}",
                        path,
                    )
                )

        def pull_and_finish(
            package: str, status: Status
        ) -> tuple[DeviceOutcome, _PendingContribution | None]:
            return _pull_and_finish(
                adb,
                device,
                info,
                package,
                status,
                dest_root,
                uninstall=uninstall,
                notify_enabled=notify_enabled,
                output_format=output_format,
                report=report,
                start=start,
            )

        def record(package: str, status: Status) -> None:
            outcome, contribution = pull_and_finish(package, status)
            outcomes.append(outcome)
            if contribution is not None:
                pending.append(contribution)

        # -- kickoff pass: visit each package's page just long enough to start
        # its install/update; everything after this is adb-only, no UI needed.
        tracked: dict[str, _Tracked] = {}
        for idx, package in enumerate(packages):
            try:
                device.ensure_connected()
                already_installed = device.is_installed(package)
                if already_installed and skip_update_check:
                    record(package, Status.SKIPPED_UPDATE_CHECK)
                    continue
                kind = "update" if already_installed else "install"
                report(package, Stage.OPENING_PLAY_STORE, kind)
                kickoff = (
                    automator.start_update(package)
                    if already_installed
                    else automator.start_install(package)
                )
                if kickoff.needs_tracking:
                    report(package, Stage.DOWNLOADING, kind)
                    tracked[package] = _Tracked(
                        kind=kind,
                        baseline_version_code=kickoff.baseline_version_code,
                        started_at=time.monotonic(),
                        retries_left=download_retries,
                    )
                else:
                    assert kickoff.status is not None
                    record(package, kickoff.status)
            except DeviceError as exc:
                outcomes.append(error_outcome(package, exc, info))
                if not device.is_connected():
                    outcomes.extend(
                        error_outcome(p, exc, info) for p in packages[idx + 1 :]
                    )
                    tracked.clear()
                    break

        # -- completion pass: poll everything still in flight, pulling each as
        # it resolves so downloading and pulling overlap.
        while tracked:
            try:
                device.ensure_connected()
            except DeviceError as exc:
                outcomes.extend(error_outcome(p, exc, info) for p in tracked)
                tracked.clear()
                break

            resolved: list[str] = []
            for package, t in tracked.items():
                try:
                    if t.kind == "install":
                        done, status = device.is_installed(package), Status.INSTALLED
                    else:
                        current = device.version_code(package)
                        done = (
                            current is not None and current != t.baseline_version_code
                        )
                        status = Status.UPDATED
                except DeviceError as exc:
                    outcomes.append(error_outcome(package, exc, info))
                    resolved.append(package)
                    continue

                if not done:
                    if (
                        download_timeout
                        and time.monotonic() - t.started_at >= download_timeout
                    ):
                        if t.retries_left > 0:
                            t.retries_left -= 1
                            logger.warning(
                                "[%s] %s: still not finished after %.0fs, "
                                "restarting (%d retry/retries left)...",
                                info.label,
                                package,
                                download_timeout,
                                t.retries_left,
                            )
                            try:
                                report(
                                    package, Stage.OPENING_PLAY_STORE, f"{t.kind}-retry"
                                )
                                kickoff = (
                                    automator.start_update(package)
                                    if t.kind == "update"
                                    else automator.start_install(package)
                                )
                                if kickoff.needs_tracking:
                                    t.baseline_version_code = (
                                        kickoff.baseline_version_code
                                    )
                                    t.started_at = time.monotonic()
                                    report(
                                        package, Stage.DOWNLOADING, f"{t.kind}-retry"
                                    )
                                else:
                                    resolved.append(package)
                                    assert kickoff.status is not None
                                    record(package, kickoff.status)
                            except DeviceError as exc:
                                resolved.append(package)
                                outcomes.append(error_outcome(package, exc, info))
                        else:
                            resolved.append(package)
                            outcomes.append(
                                error_outcome(
                                    package,
                                    DownloadTimeoutError(
                                        f"{package} did not finish downloading/"
                                        f"updating within {download_timeout:.0f}s."
                                    ),
                                    info,
                                )
                            )
                    continue
                resolved.append(package)
                try:
                    record(package, status)
                except DeviceError as exc:
                    outcomes.append(error_outcome(package, exc, info))

            for package in resolved:
                del tracked[package]
            if tracked:
                time.sleep(automator.config.poll_interval)

    except DeviceError as exc:
        device_info = (
            device.info() if device.is_connected() else DeviceInfo(device_id=device_id)
        )
        return [error_outcome(pkg, exc, device_info) for pkg in packages], []
    finally:
        if stay_on_previous is not None:
            try:
                device.put_setting(
                    "global", "stay_on_while_plugged_in", stay_on_previous or "0"
                )
            except DeviceError:
                pass
        if locale_override_active:
            try:
                device.set_app_locale(_PLAY_STORE_APP_PACKAGE, previous_play_locale)
                device.force_stop(_PLAY_STORE_APP_PACKAGE)
            except DeviceError:
                pass

    return outcomes, pending


def _warn_about_duplicate_devices(
    adb: AdbClient, targets: list[str]
) -> dict[str, DeviceInfo]:
    """Group targeted devices by the properties that decide which apk/splits
    Google Play serves and log a warning when that grouping suggests redundant
    work — either the same download twice, or one download needlessly split
    across devices.

    Returns whatever :class:`DeviceInfo` this managed to resolve, keyed by
    ``device_id`` — ``run()`` hands each one to its device's own
    ``run_for_device`` call so that thread doesn't redundantly re-fetch the
    same ``getprop``/locale round trip a second time for the same device.
    """
    infos: list[DeviceInfo] = []
    for device_id in targets:
        device = Device(adb, device_id)
        try:
            device.ensure_connected()
            infos.append(device.info())
        except Exception:  # noqa: BLE001 - best-effort check, must never break the real run
            logger.debug(
                "Could not read device info for %s while checking for duplicate "
                "devices; skipping it for this check.",
                device_id,
            )

    # Same fingerprint (abi, density bucket, langs, sdk) end-to-end: these
    # devices will pull byte-identical files, so running more than one of them
    # is redundant. Density is compared by *bucket* (e.g. "xxhdpi"), not raw
    # dpi — two devices reporting different raw density can still round to the
    # same bucket and get an identical dpi split, so raw dpi would both miss
    # real duplicates and matter less than it looks like it should.
    by_fingerprint: dict[tuple, list[DeviceInfo]] = {}
    for info in infos:
        by_fingerprint.setdefault(info.fingerprint, []).append(info)
    for (abi, density_bucket, langs, sdk), group in by_fingerprint.items():
        if len(group) > 1:
            logger.warning(
                "%d devices look identical (abi=%s, density_bucket=%s, langs=%s, "
                "sdk=%s) and will likely download the same files: %s",
                len(group),
                abi,
                density_bucket or "unknown",
                ",".join(sorted(langs)) or "unknown",
                sdk,
                ", ".join(i.label for i in group),
            )

    # Same hardware (abi, density bucket, sdk) but different configured
    # languages: these devices pull different language splits, but
    # redundantly re-pull the shared base/abi/density splits to get there —
    # configuring every language on one device instead would get the same
    # full set of splits in a single pull.
    by_hardware: dict[tuple, list[DeviceInfo]] = {}
    for info in infos:
        by_hardware.setdefault((info.abi, info.density_bucket, info.sdk), []).append(
            info
        )
    for (abi, density_bucket, sdk), group in by_hardware.items():
        distinct_langs = {info.langs for info in group}
        if len(distinct_langs) > 1:
            union = sorted(set().union(*distinct_langs))
            breakdown = ", ".join(
                f"{info.label} ({','.join(sorted(info.langs)) or 'unknown'})"
                for info in group
            )
            logger.warning(
                "%d devices share the same hardware (abi=%s, density_bucket=%s, "
                "sdk=%s) but have different configured languages, so they'll "
                "redundantly re-pull the shared splits to get different language "
                "splits: %s. Consider configuring every language (%s) on a single "
                "device instead — one pull would then cover all of them.",
                len(group),
                abi,
                density_bucket or "unknown",
                sdk,
                breakdown,
                ",".join(union) or "unknown",
            )

    return {info.device_id: info for info in infos}


def _merge_pending_contributions(
    outcomes: list[DeviceOutcome],
    contributions: list[_PendingContribution],
    dest_root: Path,
    *,
    verify: bool,
    strict_verify: bool,
    full_manifest: bool,
    output_format: OutputFormat,
    notify_enabled: bool,
    on_progress: ProgressCallback | None,
) -> None:
    """Build one merged bundle per (package, version_code) group, once every
    targeted device has resolved (this is only ever called after run()'s
    thread-pool drain loop has fully emptied — never concurrently with a
    run_for_device thread, so no locking is needed here). Mutates each
    contribution's `outcome` (already appended to `outcomes`) in place with
    the real, final `pulled_files`, and on failure `status`/`error`.
    """
    if not contributions:
        return

    def broadcast_to(
        group: list[_PendingContribution], stage: Stage, detail: str = ""
    ) -> None:
        if not on_progress:
            return
        for c in group:
            info = c.outcome.device
            on_progress(
                ProgressEvent(
                    c.raw.device_id,
                    info.label,
                    c.raw.package,
                    stage,
                    detail,
                    f"{info.abi}, {info.lang}",
                )
            )

    by_package: dict[str, list[_PendingContribution]] = {}
    for contribution in contributions:
        by_package.setdefault(contribution.raw.package, []).append(contribution)

    for package, pkg_contributions in by_package.items():
        by_version: dict[int, list[_PendingContribution]] = {}
        for contribution in pkg_contributions:
            by_version.setdefault(contribution.raw.version_code, []).append(
                contribution
            )

        if len(by_version) > 1:
            logger.warning(
                "%s: devices reported %d different version codes (%s) -- built a "
                "separate merged bundle for each; splits won't be combined across "
                "different versions.",
                package,
                len(by_version),
                ", ".join(str(v) for v in sorted(by_version)),
            )

        failed = [
            o for o in outcomes if o.package == package and o.status == Status.ERROR
        ]
        if failed:
            total = sum(1 for o in outcomes if o.package == package)
            missing = ", ".join(
                f"{o.device.label} ({o.device.abi}): {o.error}" for o in failed
            )
            logger.warning(
                "%s: merged bundle includes splits from %d/%d device(s); missing "
                "from: %s",
                package,
                total - len(failed),
                total,
                missing,
            )

        for version_code, group in by_version.items():
            # Sorted for a deterministic pick among devices whose split
            # filenames collide (see build_merged_bundle) and for
            # deterministic log-message wording run to run.
            group.sort(key=lambda c: c.raw.device_id)
            labels = [c.outcome.device.label for c in group]
            group_dir = group_staging_dir(dest_root, package, version_code)
            broadcast_to(group, Stage.PACKAGING)
            try:
                if verify:
                    broadcast_to(group, Stage.VERIFYING)
                target, pulled_files = build_merged_bundle(
                    package,
                    version_code,
                    [c.raw for c in group],
                    dest_root,
                    output_format=output_format,
                    verify=verify,
                    strict=strict_verify,
                    full=full_manifest,
                )
            except DeviceError as exc:
                # Matches run_for_device's own philosophy: only DeviceError
                # (PullError/VerificationError) is "expected" here; anything
                # else propagates and crashes the run, same as an unexpected
                # exception from run_for_device already does today.
                logger.error(
                    "%s (version %d): merge failed for %d device(s) (%s): %s",
                    package,
                    version_code,
                    len(group),
                    ", ".join(labels),
                    exc,
                )
                for c in group:
                    # This device's own install/update already succeeded --
                    # only the shared packaging step failed. Called out
                    # explicitly so ERROR-in-the-summary doesn't read as
                    # "this device's pull failed" when it didn't.
                    c.outcome.status = Status.ERROR
                    c.outcome.error = (
                        f"Merged bundle for {package} failed: {exc} (this device's "
                        f"own install/update succeeded; only local packaging failed)"
                    )
                    c.outcome.pulled_files = []
                broadcast_to(group, Stage.ERROR, detail=str(exc))
                if notify_enabled:
                    for c in group:
                        notify(
                            c.outcome.device.label, f"{package}: merge failed - {exc}"
                        )
            else:
                for c in group:
                    c.outcome.pulled_files = pulled_files
                pulled_count = sum(1 for f in pulled_files if not f.already_existed)
                logger.info(
                    "%s: merged bundle from %d device(s) (%s) -> %d file(s) pulled to %s",
                    package,
                    len(group),
                    ", ".join(labels),
                    pulled_count,
                    target,
                )
                if on_progress:
                    for c in group:
                        info = c.outcome.device
                        on_progress(
                            ProgressEvent(
                                c.raw.device_id,
                                info.label,
                                package,
                                Stage.DONE,
                                c.outcome.status.value,
                                f"{info.abi}, {info.lang}",
                                str(target),
                            )
                        )
                if notify_enabled:
                    for c in group:
                        notify(
                            c.outcome.device.label,
                            f"{package}: {c.outcome.status.value} "
                            f"({pulled_count} file(s) pulled)",
                        )
            finally:
                shutil.rmtree(group_dir, ignore_errors=True)


def _drain_batch(
    done: set,
    outcomes: list[DeviceOutcome],
    all_contributions: list[_PendingContribution],
) -> list[Exception]:
    """Collect every finished future in ``done`` into ``outcomes``/
    ``all_contributions``, in place.

    ``done`` (from ``concurrent.futures.wait``) is an *unordered* set, so an
    unexpected exception from one device's future must not stop a sibling
    future's already-computed result -- also sitting right there in the same
    batch -- from being collected: any exception raised by ``future.result()``
    (a genuine bug in ``run_for_device``, not a ``DeviceError`` it already
    turned into a normal error outcome -- those never raise) is collected and
    returned instead of raised immediately, so every future in the batch is
    always drained first. A future ``run()`` itself already cancelled (see
    the ``KeyboardInterrupt`` handling below) is skipped rather than treated
    as an error.
    """
    exceptions: list[Exception] = []
    for future in done:
        if future.cancelled():
            continue
        try:
            device_outcomes, contributions = future.result()
        except Exception as exc:  # noqa: BLE001 - returned to the caller, not swallowed
            exceptions.append(exc)
            continue
        outcomes.extend(device_outcomes)
        all_contributions.extend(contributions)
    return exceptions


def run(
    packages: str | list[str],
    dest_root: Path,
    *,
    device_ids: list[str] | None = None,
    uninstall: bool = False,
    max_workers: int | None = None,
    notify_enabled: bool = False,
    verify: bool = True,
    strict_verify: bool = False,
    full_manifest: bool = False,
    output_format: OutputFormat = OutputFormat.APKS,
    adb_path: str | None = None,
    automation_config: AutomationConfig | None = None,
    skip_existence_check: bool = False,
    keep_screen_on: bool = True,
    download_timeout: float = 300.0,
    download_retries: int = 1,
    skip_duplicate_check: bool = False,
    skip_update_check: bool = False,
    force_locale: str | None = None,
    on_progress: ProgressCallback | None = None,
) -> RunSummary:
    """Pull every package in ``packages`` from every targeted device concurrently."""
    package_list = [packages] if isinstance(packages, str) else list(packages)
    for package in package_list:
        validate_package_name(package)
        if not skip_existence_check:
            check_package_exists(package)
    if force_locale is not None and get_locale(force_locale) is None:
        raise UnsupportedLocaleError(
            f"--force-locale {force_locale!r} isn't a locale apkpull supports "
            f"(supported: {', '.join(supported_languages())})."
        )

    adb = AdbClient(adb_path)
    targets = device_ids or adb.list_devices()
    if not targets:
        raise NoDevicesFoundError(
            "No devices found! At least one device must be connected."
        )
    logger.info(
        "%d device(s) targeted, %d package(s) each.", len(targets), len(package_list)
    )
    device_infos: dict[str, DeviceInfo] = {}
    if len(targets) > 1 and not skip_duplicate_check:
        device_infos = _warn_about_duplicate_devices(adb, targets)

    if on_progress:
        # Device label isn't known yet (that needs a live adb round-trip,
        # done per-device once its own thread starts) — device_id doubles as
        # the initial label so the renderer has a stable row to show
        # immediately, updated to the friendly label once resolved.
        for device_id in targets:
            for package in package_list:
                on_progress(ProgressEvent(device_id, device_id, package, Stage.QUEUED))

    # Best-effort: clean up anything a previous, crashed run left staged but
    # never merged. Safe to do before any device thread starts -- nothing
    # below can have written into this run's own staging dirs yet. Some
    # callers (mainly tests that mock run_for_device and never touch the
    # filesystem) pass dest_root=None despite the type hint -- guard rather
    # than break that existing convention.
    if dest_root is not None:
        shutil.rmtree(staging_root(dest_root), ignore_errors=True)

    outcomes: list[DeviceOutcome] = []
    all_contributions: list[_PendingContribution] = []
    # Not a `with` block: ThreadPoolExecutor.__exit__ calls shutdown(wait=True)
    # unconditionally, which would block on already-running devices even after
    # we've deliberately stopped waiting for them below on Ctrl+C.
    pool = ThreadPoolExecutor(
        max_workers=max_workers or len(targets), thread_name_prefix="apkpull"
    )
    futures = {
        pool.submit(
            run_for_device,
            adb,
            device_id,
            package_list,
            dest_root,
            uninstall=uninstall,
            notify_enabled=notify_enabled,
            output_format=output_format,
            automation_config=automation_config,
            keep_screen_on=keep_screen_on,
            download_timeout=download_timeout,
            download_retries=download_retries,
            skip_update_check=skip_update_check,
            force_locale=force_locale,
            on_progress=on_progress,
            device_info=device_infos.get(device_id),
        ): device_id
        for device_id in targets
    }
    pending = set(futures)
    try:
        while pending:
            # Poll with a short timeout instead of blocking indefinitely: an
            # untimed wait on thread-pool futures doesn't get interrupted by
            # SIGINT at all (confirmed hands-on against a real run) — Ctrl+C
            # would otherwise do nothing until every device finished or timed
            # out on its own, which could be minutes away.
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            exceptions = _drain_batch(done, outcomes, all_contributions)
            if exceptions:
                # A genuine bug in run_for_device, not a per-device DeviceError
                # (those never reach here as an exception). Shut down the same
                # way Ctrl+C does below -- cancel whatever hasn't started,
                # wait for whatever's already running (can't be force-killed,
                # but is bounded by its own timeouts), close the pool -- so
                # this doesn't abandon still-running device threads or lose
                # any sibling device's already-collected result, then surface
                # the failure instead of silently dropping it.
                for future in pending:
                    future.cancel()
                while pending:
                    done, pending = wait(
                        pending, timeout=1.0, return_when=FIRST_COMPLETED
                    )
                    exceptions.extend(_drain_batch(done, outcomes, all_contributions))
                pool.shutdown(wait=False)
                raise exceptions[0]
        pool.shutdown(wait=False)
    except KeyboardInterrupt:
        for future in pending:
            future.cancel()  # only takes effect for devices not yet started
        logger.warning(
            "Interrupted — not starting any more device(s). Waiting for %d "
            "already in flight to finish (they can't be stopped mid-call, "
            "but are already bounded by their own timeouts).",
            len(pending),
        )
        while pending:
            done, pending = wait(pending, timeout=1.0, return_when=FIRST_COMPLETED)
            for exc in _drain_batch(done, outcomes, all_contributions):
                # Same "don't lose the rest of the batch" reasoning as the
                # normal path above -- but here the original KeyboardInterrupt
                # is what must keep propagating (the `raise` at the end of
                # this block), so an unexpected exception from a straggling
                # future is logged rather than re-raised in its place.
                logger.error(
                    "Unexpected error from a device thread during shutdown: %s", exc
                )
        pool.shutdown(wait=False)
        # Still merge whatever raw pulls did finish before the interrupt --
        # strictly better than leaving them staged and unused, and it's pure
        # local-file work so it doesn't meaningfully delay the interrupt.
        _merge_pending_contributions(
            outcomes,
            all_contributions,
            dest_root,
            verify=verify,
            strict_verify=strict_verify,
            full_manifest=full_manifest,
            output_format=output_format,
            notify_enabled=notify_enabled,
            on_progress=on_progress,
        )
        raise

    _merge_pending_contributions(
        outcomes,
        all_contributions,
        dest_root,
        verify=verify,
        strict_verify=strict_verify,
        full_manifest=full_manifest,
        output_format=output_format,
        notify_enabled=notify_enabled,
        on_progress=on_progress,
    )
    return RunSummary(outcomes=outcomes)
