<h2 align="center">
  <img src="https://raw.githubusercontent.com/david-lev/apkpull/main/assets/logo.svg" width="40" height="40" alt="apkpull logo"/>
  <a href="https://github.com/david-lev/apkpull">apkpull</a> • Download Android apps from Google Play as one installable bundle
</h2>

<p align="center">
  <a href="https://pypi.org/project/apkpull/"><img src="https://img.shields.io/pypi/v/apkpull?color=%2334D058&label=pypi" alt="PyPI Version"/></a>
  <a href="https://pepy.tech/project/apkpull"><img src="https://static.pepy.tech/badge/apkpull" alt="Downloads"/></a>
  <a href="pyproject.toml"><img src="https://img.shields.io/badge/python-3.10%2B-3776ab?color=%2334D058" alt="Python Versions"/></a>
  <a href="https://github.com/david-lev/apkpull/actions/workflows/tests.yml"><img src="https://img.shields.io/github/actions/workflow/status/david-lev/apkpull/tests.yml?label=tests" alt="Tests"/></a>
  <a href="https://results.pre-commit.ci/latest/github/david-lev/apkpull/main"><img src="https://results.pre-commit.ci/badge/github/david-lev/apkpull/main.svg" alt="pre-commit.ci status"/></a>
  <a href="https://github.com/david-lev/apkpull/blob/main/LICENSE"><img src="https://img.shields.io/github/license/david-lev/apkpull?color=%2334D058" alt="License"/></a>
  <a href="https://www.codefactor.io/repository/github/david-lev/apkpull/overview/main"><img src="https://www.codefactor.io/repository/github/david-lev/apkpull/badge/main" alt="Code Quality"/></a>
</p>

**apkpull drives Google Play like a person would** — installs or updates an app on real, adb-connected devices or
emulators, then pulls the base APK and every split into one verified, installable `.apks` bundle, powered by
[apkfile](https://github.com/david-lev/apkfile). Point it at several devices at once and it merges their splits
into a single artifact anyone can install, whatever their phone's CPU, screen, or language.

```bash
apkpull com.whatsapp                                        # pull one app from every connected device
apkpull com.whatsapp,com.spotify.music -d ~/Apps            # pull several apps, into a custom folder
```

## Install

```bash
pip install -U apkpull
# or
uv tool install apkpull   # installs the `apkpull` command standalone
uv add apkpull            # ...or as a dependency, to use it as a library (see "As a library" below)
```

Requires Python 3.10+ and [ADB](https://developer.android.com/studio/command-line/adb) on your `PATH`. That's it —
`apkpull com.whatsapp` is ready to run. See [Development](#development) below for setting up a local clone instead.

## Why run more than one device?

Google Play doesn't hand out one universal APK — every install is built from a **base APK** plus a handful of
**splits**, chosen to match that specific device: its CPU architecture (ABI), screen density, and every language
it has configured. Pull from a single phone and you only get *that phone's* splits — perfectly installable on that
phone, but potentially missing pieces someone else's phone needs: a different ABI, a higher-density screen, a
language you don't happen to have installed.

apkpull's answer is to run against several devices at once and merge the *union* of everything they each pulled
into one bundle (see [Cross-device merging](#cross-device-merging) below) — so instead of "the splits my phone
happened to get," you end up with "every split any of these devices got," installable by a much wider set of real
phones. Three cheap emulators (or devices) get you almost all the way there:

- **Two ABIs** — one `armeabi-v7a` (32-bit) and one `arm64-v8a` (64-bit). Between them they cover virtually every
  Android phone in the wild; skip `x86`/`x86_64` unless you specifically care about Chromebooks or x86 tablets,
  which few apps bother splitting for anyway.
- **Two screen densities** — Android buckets every real screen into one of a handful of density tiers
  (`ldpi`/`mdpi`/`hdpi`/`xhdpi`/`xxhdpi`/`xxxhdpi`), and Play only ever serves the one closest to a device's actual
  dpi. **xxhdpi (~480dpi)** and **xxxhdpi (~640dpi)** between them cover most phones sold in the last several
  years, from mid-range through current flagships — set one emulator's AVD screen density to each.
- **As many languages as possible, all on one device** — Play fetches one split per *installed* language, so
  running a separate device per language is wasted effort. Add every language you care about to a single device
  (Settings → System → Languages) instead, and one pull covers all of them — apkpull already unions a device's
  *full* configured language list into the bundle, not just its primary locale.

This is also exactly what apkpull's own duplicate-device detection is built around: two devices that agree on ABI,
density bucket, SDK, and configured languages will just pull the same files twice, so apkpull warns about it (but
still processes both); two devices that agree on everything *except* language get flagged too, with a suggestion
to fold them into one multi-language device instead — precisely the setup recommended above. See
[Duplicate/redundant devices](#reliability) below for the exact mechanics.

## Setting up a device or emulator

apkpull needs at least one adb-reachable device or emulator with **Google Play installed and signed in**.

### Real device

1. Enable Developer Options (tap Settings → About phone → Build number 7 times), then enable USB debugging
   inside Developer Options.
2. Plug it in over USB (or set up [wireless debugging](https://developer.android.com/tools/adb#wireless)) and
   accept the "Allow USB debugging?" prompt on the device.
3. `adb devices` should list it as `device` (not `unauthorized` — re-check the on-device prompt if so).

### Emulator (AVD)

Any AVD works as long as its system image includes **Play Store**, not just Play Services — look for images
tagged `Google Play` in Android Studio's AVD wizard, or `google_apis_playstore` on the command line (a plain
`google_apis` image has no Play Store app at all, and apkpull can't do anything without one).

**Android Studio** (easiest): Tools → Device Manager → Create Device → pick a phone profile → pick a system
image with a **Play Store** icon next to it → Finish, then press ▶ to boot it.

**Command line**, if you'd rather not open Android Studio (`sdkmanager`/`avdmanager`/`emulator` live under your
SDK root, e.g. `$ANDROID_HOME/cmdline-tools/latest/bin` and `$ANDROID_HOME/emulator` — add those to `PATH`, or
run them with their full path):

```bash
# list installable system images, install one with a Play Store (adjust api level/abi as needed)
sdkmanager --list | grep google_apis_playstore
sdkmanager "system-images;android-34;google_apis_playstore;arm64-v8a"   # arm64-v8a on Apple Silicon, x86_64 on Intel/most Linux

avdmanager create avd -n apkpull -k "system-images;android-34;google_apis_playstore;arm64-v8a" -d pixel_6
emulator -avd apkpull
```

Either way, confirm it shows up before running apkpull:

```bash
adb devices        # should list it as `device`
apkpull com.whatsapp -v
```

### Hardware acceleration

The emulator is only fast with real hardware virtualization behind it — apkpull's UI automation does several
`uiautomator dump`/screenshot round-trips per screen, so this matters a lot in practice:

- **macOS**: uses Apple's Hypervisor.framework (HVF) automatically, no setup needed. Check with
  `sysctl kern.hv_support` (`1` = supported).
- **Linux**: needs KVM — `sudo apt install qemu-kvm` (Debian/Ubuntu) or equivalent, and your user in the `kvm`
  group. Check with `ls -l /dev/kvm`.
- **Windows**: uses WHPX (Windows Hypervisor Platform) or Hyper-V — enable via "Turn Windows features on or
  off" → Windows Hypervisor Platform.

Without one of these, the emulator falls back to full software emulation — it'll still work, just painfully
slowly for anything UI-automation-heavy.

### Docker?

Short answer: great on Linux, not currently useful on macOS.

On a **Linux host**, running the emulator inside Docker with hardware acceleration is a well-trodden path —
pass `--device /dev/kvm` into the container and it gets near-native speed. This is exactly how CI systems do it
(e.g. [`reactivecircus/android-emulator-runner`](https://github.com/ReactiveCircus/android-emulator-runner) for
GitHub Actions, or [`budtmo/docker-android`](https://github.com/budtmo/docker-android) as a standalone image);
apkpull just needs `adb connect`/`adb devices` to see whatever container(s) you bring up, same as any device.

On **macOS**, there's no equivalent: Docker Desktop for Mac runs containers inside its own lightweight Linux VM,
and that inner VM doesn't expose nested virtualization (no `/dev/kvm`) or HVF passthrough to containers — a
containerized emulator would fall back to software emulation, i.e. *slower* than just running it natively. On
Mac, running the emulator directly on the host (as above) already gets you HVF acceleration for free; Docker
wouldn't add anything here. If you need many parallel emulators for scale rather than raw speed, a Linux
box/CI runner (or a cloud device farm) is the more productive direction than fighting macOS's Docker sandboxing.

## Usage

```bash
apkpull com.whatsapp
apkpull com.whatsapp,com.spotify.music -d ~/Documents/my_apks --uninstall-after -v
apkpull com.whatsapp --devices emulator-5554,emulator-5556 --json
```

```
usage: apkpull [-h] [-d DIR] [--uninstall-after] [--devices ID1,ID2,...]
               [--max-workers MAX_WORKERS] [--max-poll-rounds MAX_POLL_ROUNDS]
               [--unlock-timeout SECONDS] [--download-timeout SECONDS]
               [--download-retries N] [--format {apks,zip,folder}]
               [--notify] [--no-keep-screen-on] [--no-verify] [--strict-verify]
               [--full-manifest]
               [--skip-existence-check] [--skip-duplicate-check] [--skip-update-check]
               [--no-live] [--adb-path ADB_PATH] [--json] [-v] [--version]
               packages

  packages              Package name, e.g. com.whatsapp. Comma-separated for more than
                        one, e.g. com.whatsapp,com.spotify.music — apps on the same
                        device download concurrently, same as tapping Install on each
                        from Google Play.
  -d, --dest DIR        Directory to pull files into (default: ~/Downloads/APKpull)
  --uninstall-after     Uninstall the app after pulling it.
  --devices ID1,ID2,... Comma-separated device ids to target (default: every device adb sees).
  --max-workers N       Max devices processed concurrently.
  --max-poll-rounds N   UI-polling rounds before giving up on a device.
  --unlock-timeout SEC  Seconds to wait for a locked device to unlock (0 = forever). Default: 300.
  --download-timeout SEC
                        Seconds to wait for a download/update to finish before retrying
                        or giving up on it (0 = forever). Default: 300.
  --download-retries N  Times to restart a timed-out download/update before reporting
                        it as failed. Default: 1.
  --format {apks,zip,folder}
                        Output format (default: apks). See "Output formats" below.
  --notify              Send native desktop notifications (macOS/Linux).
  --no-keep-screen-on   Don't force the screen to stay awake while plugged in during the
                        pull (default: on; the device may lock and interrupt automation).
  --no-verify           Skip apkfile verification of pulled apks.
  --strict-verify       Treat a verification mismatch as a device failure.
  --full-manifest       Write the full apkfile manifest (every permission's AOSP detail,
                        exported/deep-link components, size breakdown, dex info, full
                        certificate fields) to manifest.json instead of the trimmed
                        default. No effect with --no-verify.
  --skip-existence-check
                        Don't do the advisory pre-check of whether the package exists on
                        Google Play (see below) before touching devices.
  --skip-duplicate-check
                        Don't warn about devices that look identical or share hardware
                        but differ only in configured languages (see below).
  --skip-update-check   For an already-installed package, pull whatever's currently on
                        the device instead of checking Google Play for an update first
                        (see below) — faster, but may pull a stale version.
  --no-live             Don't show the live per-device/per-package status table — just
                        plain, unbounded log lines. Automatic when stderr isn't a real
                        terminal.
  --adb-path PATH       Path to the adb executable (default: search PATH).
  --json                Print the run summary as JSON instead of text.
  -v, -vv               Verbose logging: -v for info, -vv for debug.
```

Exit code is `0` when every targeted device succeeds, otherwise the number of failed devices (capped at `9`); `50`
if no devices were found at all.

## As a library

```python
from apkpull import OutputFormat, run

summary = run(
    ["com.whatsapp", "com.spotify.music"],  # a single package name also works
    "~/Documents/my_apks",
    uninstall=True,
    output_format=OutputFormat.FOLDER,
)
for outcome in summary.outcomes:
    print(outcome.device.model, outcome.package, outcome.status, outcome.destination)
```

`run()` returns a `RunSummary` dataclass (`.as_dict()` for JSON) — see `apkpull/models.py` for the full shape.

## How it works

- **Why UI automation, and why text instead of resource-ids or coordinates**: apkpull needs *something* stable to
  find a button on. Raw screen coordinates break the moment the layout shifts — a different screen size, or Play
  Store itself pushing an update that reflows a screen; resource-ids would be the normal fix for that, but Google
  Play's own UI exposes none on its buttons at all — confirmed by dumping the live UI tree, every text node's
  `resource-id` comes back empty. What *is* stable is a button's visible, localized text, so that's what apkpull
  matches on: dump the screen with `uiautomator`, find a node by its text for the device's language, tap its
  bounds — the same way a person would. English, Hebrew, Spanish, French and Russian are built in; add a language
  by extending the table in `apkpull/locales.py` — `scripts/extract_play_strings.sh` helps bootstrap most of a new
  locale's strings straight from Google Play's own APK (see its header comment for how, and its limits).
- Each connected device runs on its own thread, so multiple devices — potentially with different
  architectures/densities/languages — download and pull in parallel. apkpull waits for every targeted device to
  finish a given package, then merges the union of all their distinct splits into one bundle (see
  ["Why run more than one device?"](#why-run-more-than-one-device) above and
  [Cross-device merging](#cross-device-merging) below) instead of one incomplete bundle per device.
- Multiple packages on the *same* device also download concurrently rather than one at a time: apkpull briefly
  visits each package's Play Store page just long enough to tap Install/Update, then tracks every in-flight
  package purely over adb (`pm path`/`dumpsys package`) and pulls each as it finishes — no UI polling involved,
  since Google Play's own download manager genuinely overlaps downloads once they're started (confirmed by
  watching two real installs queue and finish out of tap order on a live device). One tradeoff: because apkpull
  isn't camped on any single package's page anymore, it can no longer auto-retry a download a human cancelled
  by hand mid-run — only a device going offline is still recovered from mid-flight.
- For a package already installed on the device, apkpull's default is to check Google Play for an update first and
  pull whatever that leaves installed (the existing version if already up to date, the new one if an update was
  just applied) — `--skip-update-check` skips that check entirely for such a package: no Play Store launch, no UI
  polling, straight to pulling whatever's on the device right now. Faster and more reliable (a whole class of UI
  automation just doesn't happen for that package), at the cost of possibly pulling a stale version — reported as
  `skipped_update_check` rather than `already_up_to_date`, since apkpull never actually checked.
- Every pulled app is packaged into a single `<package>-<version_code>` artifact — base apk + every split, always
  zipped and verified with `apkfile` first regardless of format (see below), only *materialized* differently at
  the end. That's what makes it immediately useful on its own: install it straight from `apkfile`
  (`ApksFile(path).install()`), hand it to [SAI](https://github.com/Aefyr/SAI) on another Android device, or just
  unzip it — instead of a directory tree of loose files only that one pull could make sense of.
- The zip is re-opened locally with `apkfile` and cross-checked against what adb reported on-device (package
  name, version code) — catching a truncated pull, or a bug in the zipping step, that `adb pull`'s own exit code
  and a successful zip write would both miss. A `manifest.json` is written with the aggregated metadata:
  permissions, features, ABIs, languages, signing certificates and more, unioned across the base apk and every
  split — trimmed by default to keep it digestible (dropping per-permission AOSP detail, exported/deep-link
  component lists, size breakdown, dex info, and most certificate fields); pass `--full-manifest` for all of it.

### Output formats (`--format`)

| Format (default **apks**) | Layout |
|---|---|
| `apks` | `<package>-<version_code>.apks` — a [bundletool](https://developer.android.com/tools/bundletool)/SAI-format zip (`meta.sai_v2.json` + base + splits) |
| `zip` | `<package>-<version_code>.zip` — byte-identical contents, just a familiar extension for tools that expect a plain zip |
| `folder` | `<package>-<version_code>/` — extracted: `base.apk`, each split, and `manifest.json` as loose files |

All output lands flat in one destination directory — one artifact + one `manifest.json` per (package, version)
(inside the folder for `folder`, a `.manifest.json` sidecar otherwise), plus a loose OBB file on the rare app
that ships one (inside the folder too, for `folder`):
```
APKpull/
├── com.whatsapp-243119027.apks
├── com.whatsapp-243119027.manifest.json
├── tfilon.tfilon-41.zip
├── tfilon.tfilon-41.manifest.json
└── com.some.game-9/
    ├── base.apk
    ├── config.arm64_v8a.apk
    ├── manifest.json
    └── main.9.com.some.game.obb
```
An artifact that already exists locally is never rebuilt — a rerun of the same package/version skips straight
past every device without touching any of them.

### Cross-device merging

apkpull waits for every targeted device to finish a given package (success or failure) before packaging it, then
merges the union of all their distinct splits — and any OBB — into one bundle. Devices reporting different
`version_code`s for the same package can't be merged (Play Store can stage rollouts), so those get one bundle
per version instead. If some devices fail while others succeed, apkpull still builds the bundle from whoever
succeeded and logs a warning naming which device(s)/architecture are missing from it — a device's own outcome can
end up `error` purely because a sibling's contribution (or the shared bundle's strict-verify check) broke the
merge, even though that device's own install/update succeeded; the error message says so explicitly. Raw pulls
are staged under a `.apkpull-staging/` directory inside the destination while a group is still in flight, cleaned
up once its bundle is built (or the merge fails) — and best-effort swept at the start of every run in case a
previous one crashed before cleaning up its own.

### Reliability

- **Ctrl+C**: confirmed hands-on that the naive `ThreadPoolExecutor` + `as_completed()` pattern silently swallows
  SIGINT — an untimed wait on thread-pool futures just doesn't get interrupted, so Ctrl+C used to do nothing until
  every device finished or timed out on its own (which, with default timeouts, could be minutes). Fixed by
  polling with a 1-second timeout instead of blocking indefinitely, so the interrupt is noticed promptly. On
  Ctrl+C, apkpull stops launching any more work (cancels devices that haven't started yet), prints a clear
  message, and exits with `130` (the standard shell convention) instead of a raw traceback — but a device already
  mid-adb-call can't be safely force-killed, so apkpull waits for it to reach its own natural stopping point
  (bounded by its existing `--download-timeout`/`--unlock-timeout`/etc.) rather than abandoning it uncleanly.
- **Foreground detection**: apkpull needs to know whether the device is still showing Google Play (vs. having
  navigated away) to decide whether to relaunch it. This used to read `dumpsys activity activities`'
  `mResumedActivity` field — which turns out to not exist at all on Android 14 (API 34) and up, replaced by
  `topResumedActivity`/`ResumedActivity:`. With that field silently missing, apkpull would conclude it was
  *never* on Google Play and relaunch the details page on every single poll, even mid-download — resetting the
  page and derailing the install. Fixed by reading `dumpsys window`'s `mCurrentFocus` instead, which has been a
  stable single-line answer across the versions checked.
- **Package existence**: before touching any device, apkpull does an advisory-only check of whether the package
  has a listing on the Play Store *website* (`--skip-existence-check` turns it off). It only ever warns, never
  aborts the run — a confirmed 404 there doesn't reliably mean the package can't be installed on the actual
  target device, since that request runs from apkpull's host machine's own network/region, not the device's Play
  Store account's region. A region-locked app (most banking apps, for instance — verified hands-on against a real
  one) can easily 404 the web listing from one country while installing fine on a device signed in with the right
  region. The on-device automation is the real source of truth, and it now recognizes two more screens instead of
  falling through to a slow, unhelpful `AutomationTimeoutError`: a dedicated "item not found" page (confirmed
  hands-on), and — more generally — Play's red warning-triangle icon, which reliably pairs with a plain-text error
  message on the details page (confirmed hands-on for the region-restricted screen). Any banner using that icon
  that apkpull doesn't have a specific rule for still gets caught immediately, with the actual on-screen text
  surfaced in the error instead of a generic timeout.
- **Insufficient device storage**: confirmed hands-on against a genuinely low-storage emulator, Play Store's "Not
  enough storage" dialog only ever appears *after* tapping Install/Update — never on the details page beforehand
  — so apkpull now takes one extra look right after that tap specifically to catch it (and, by the same change,
  any other post-tap dialog), instead of assuming a download silently started in the background and polling adb
  forever for an install that will never happen.
- **Paid apps**: detected by a price tag (e.g. `$4.99`) anywhere on the details page, which is driven by the
  signed-in Google account's *billing region*, not the display language — confirmed hands-on: a Russian-language
  page for a paid app still showed its price in Shekels, matching the test account's actual billing country. So
  this needs no per-locale strings, just currency-symbol coverage (`₪ $ € £ ₽ ¥ ₹`).
- **Offline, two different ways**: Play Store shows *two* distinct screens for no network, confirmed hands-on —
  a themed "You're offline" browsing page (usually hit navigating straight to a package's details page) and a
  separate plainer "Something went wrong" / "No internet connection..." page with a "Try again" button (seen via
  other navigation paths, e.g. from the store's home feed). Both are caught and raise the same error, since it's
  the same underlying condition either way.
- **Locked devices**: apkpull waits for a locked device to be unlocked before starting automation, polling
  `--unlock-timeout` seconds (default 300, `0` = forever) before giving up on that device — without this, a
  single locked device would hang the entire run forever, since it waits for every targeted device before
  printing a summary.
- **Disconnected devices**: every polling loop (waiting for a button, waiting for install/update to finish,
  waiting for unlock) re-checks the connection each iteration and fails that device cleanly the moment it drops,
  rather than hanging or crashing the whole run — a device going offline mid-pull is reported as a normal
  per-device failure, and every other device keeps running.
- **Stalled downloads**: the adb-only poll that waits for a download/update to finish (see above) is bounded by
  `--download-timeout` seconds (default 300, `0` = forever); on timeout apkpull restarts that package's kickoff up
  to `--download-retries` times (default 1) before reporting it as failed, so one stuck download can't hang a
  device — or, with `0`/no other in-flight packages, the whole run — forever.
- **Duplicate/redundant devices**: when targeting more than one device, apkpull warns (but still processes every
  device) about two kinds of overlap, based on the exact properties Google Play actually uses to pick which
  apk/splits to serve — ABI, screen density *bucket*, SDK version, and the *full set* of languages configured on
  the device (not just the primary one — Play fetches a language split per installed language, so a device with
  three languages configured contributes three language splits, not one):
    - Devices matching on **all four** will download byte-identical files — a straightforward waste, since running
      one of them would produce the exact same contribution to the merged bundle as running both.
    - Devices matching on ABI/density/SDK but with **different configured languages** will each pull different
      language splits, but redundantly re-pull the shared base/ABI/density splits to get there — apkpull suggests
      configuring every language on a single device instead (see
      ["Why run more than one device?"](#why-run-more-than-one-device) above), so one pull covers all of them
      without repeating the shared splits per language.

  Density is compared by *bucket* (`ldpi`/`mdpi`/`tvdpi`/`hdpi`/`xhdpi`/`xxhdpi`/`xxxhdpi` — Play only ever serves
  one of these, never a device's exact raw dpi), not the raw `ro.sf.lcd_density` value, using `apkfile`'s
  `DensityBucket` and nearest-match-by-distance: two devices reporting different raw density (e.g. 420 vs 439) can
  round to the same bucket and get an identical dpi split, so comparing raw density would both miss real
  duplicates and flag devices whose difference doesn't actually matter.

  `--skip-duplicate-check` turns this off entirely (no warning, and no extra per-device `getprop` round-trip to
  check for one) for setups where the overlap is intentional — e.g. deliberately running two identical devices
  just to compare timing, or a fleet where every device really is meant to be a like-for-like clone.
- Run with `-vv` to see every adb call and UI-polling decision. If automation gets stuck (an unrecognized screen
  after `--max-poll-rounds` polls), a screenshot + UI dump are saved for a bug report (path logged at the time).

## Development

Uses [uv](https://docs.astral.sh/uv/) for dependency management:

```bash
git clone https://github.com/david-lev/apkpull.git
cd apkpull
uv sync
uv run pre-commit install
```

Run the CLI from a clone with `uv run apkpull ...` (no need to `pip install -e .` first).

```bash
uv run pytest                       # full test suite (unit + integration)
uv run pytest -m "not integration"  # unit tests only — no device required
uv run pytest -m integration        # only the tests that need a real adb device/emulator

uv run ruff check .                 # lint
uv run ruff format .                # format
uv run ty check                     # type check
uv run pre-commit run --all-files   # everything pre-commit enforces
```

The integration tests only ever target `com.android.vending` (the Play Store app, always already installed), so
they never install/update/uninstall anything on your device — they exercise device discovery, the "already up to
date" automation path, real pulling, and `apkfile` verification against genuine adb output. They skip themselves
automatically when no device is connected, so plain `uv run pytest` is always safe to run.

CI (`.github/workflows/tests.yml`) runs the unit test suite across Python 3.10–3.14 on Linux, plus 3.10/3.14
(min/max) on macOS and Windows, and a separate `lint` job runs `ruff check`, `ruff format --check`, and `ty check`.

## Legacy bash version

The original single-file [`apkpull.sh`](./apkpull.sh) still works and needs nothing but `bash` + `adb`, but is no
longer maintained — the Python CLI above is the supported tool going forward.
