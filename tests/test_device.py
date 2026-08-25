from apkpull.device import Device

from .helpers import FakeAdb


def make_device(**shell_responses) -> tuple[Device, FakeAdb]:
    adb = FakeAdb()
    adb.shell_responses.update(shell_responses)
    return Device(adb, "fake-1"), adb


def test_info_parses_getprop_output():
    device, _ = make_device(
        getprop="[ro.product.model]: [Pixel 7]\n[ro.product.cpu.abi]: [arm64-v8a]\n"
        "[persist.sys.locale]: [en-US]\n[ro.build.version.sdk]: [34]\n"
        "[ro.sf.lcd_density]: [420]\n"
    )
    info = device.info()
    assert info.model == "Pixel 7"
    assert info.abi == "arm64-v8a"
    assert info.lang == "en-US"
    assert info.langs == {"en"}
    assert info.label == "Pixel 7"
    assert info.sdk == 34
    assert info.density == 420
    assert info.density_bucket == "xxhdpi"


def test_info_falls_back_to_qemu_density_prop():
    device, _ = make_device(getprop="[qemu.sf.lcd_density]: [440]\n")
    info = device.info()
    assert info.density == 440
    assert info.density_bucket == "xxhdpi"


def test_info_defaults_to_unknown_when_missing():
    device, _ = make_device(getprop="")
    info = device.info()
    assert info.model == info.abi == info.lang == "unknown"
    assert info.langs == frozenset()
    assert info.label == "fake-1"
    assert info.sdk is None
    assert info.density is None
    assert info.density_bucket is None


def test_info_maps_raw_density_to_nearest_bucket():
    """Real devices rarely report a density that lands exactly on a standard
    bucket -- these are values seen in the wild that must round to their
    nearest bucket, not just match an exact one."""
    cases = {
        120: "ldpi",
        160: "mdpi",
        213: "tvdpi",
        240: "hdpi",
        320: "xhdpi",
        360: "xhdpi",  # common real device value, between xhdpi(320) and xxhdpi(480)
        480: "xxhdpi",
        560: "xxhdpi",  # common real device value, between xxhdpi(480) and xxxhdpi(640)
        640: "xxxhdpi",
    }
    for raw_dpi, expected_bucket in cases.items():
        device, _ = make_device(getprop=f"[ro.sf.lcd_density]: [{raw_dpi}]\n")
        assert device.info(refresh=True).density_bucket == expected_bucket, raw_dpi


def test_info_is_cached_until_refresh():
    device, adb = make_device(getprop="[ro.product.model]: [A]\n")
    device.info()
    adb.shell_responses["getprop"] = "[ro.product.model]: [B]\n"
    assert device.info().model == "A"
    assert device.info(refresh=True).model == "B"


def test_lang_prefers_persist_sys_locales_over_singular():
    device, _ = make_device(
        getprop="[persist.sys.locales]: [fr-FR,en-US]\n[persist.sys.locale]: [en-US]\n"
    )
    assert device.info().lang == "fr-FR"


def test_langs_collects_every_configured_locale_not_just_the_primary():
    """Play Store fetches a language split per *installed* language, not just the
    primary one — a device with fr-FR,en-US,he-IL configured needs all three
    tracked, even though `lang` (automation) only cares about the first."""
    device, _ = make_device(getprop="[persist.sys.locales]: [fr-FR,en-US,he-IL]\n")
    info = device.info()
    assert info.lang == "fr-FR"
    assert info.langs == {"fr", "en", "he"}


def test_lang_falls_back_to_settings_when_persist_props_unset():
    """Regression: apkfile's install.py confirmed hands-on that a fresh/headless emulator can
    leave persist.sys.locale(s) completely empty even though it's fully booted."""
    device, _adb = make_device(
        getprop="[ro.product.model]: [Emulator]\n",
        **{"settings get system system_locales": "de-DE\n"},
    )
    assert device.info().lang == "de-DE"


def test_lang_falls_back_to_ro_product_locale_as_last_resort():
    device, _ = make_device(
        getprop="[ro.product.locale]: [ja-JP]\n",
        **{"settings get system system_locales": "null\n"},
    )
    assert device.info().lang == "ja-JP"


def test_lang_unknown_when_every_source_is_empty():
    device, _ = make_device(getprop="")
    assert device.info().lang == "unknown"


def test_is_installed_true_when_pm_path_has_output():
    device, _ = make_device(
        **{"pm path com.whatsapp": "package:/data/app/com.whatsapp/base.apk\n"}
    )
    assert device.is_installed("com.whatsapp") is True


def test_is_installed_false_when_pm_path_empty():
    device, _ = make_device(**{"pm path com.whatsapp": ""})
    assert device.is_installed("com.whatsapp") is False


def test_is_disabled_matches_exact_package():
    device, _ = make_device(
        **{"pm list packages -d": "package:com.other\npackage:com.whatsapp\n"}
    )
    assert device.is_disabled("com.whatsapp") is True
    assert device.is_disabled("com.wha") is False


def test_apk_paths_strips_package_prefix():
    device, _ = make_device(
        **{
            "pm path com.whatsapp": "package:/data/app/base.apk\npackage:/data/app/split_config.en.apk\n"
        }
    )
    assert device.apk_paths("com.whatsapp") == [
        "/data/app/base.apk",
        "/data/app/split_config.en.apk",
    ]


def test_version_code_name_min_sdk_parsed_from_dumpsys():
    dumpsys = "    versionCode=451 minSdk=21\n    versionName=1.2.3\n"
    device, _ = make_device(**{"dumpsys package com.app": dumpsys})
    assert device.version_code("com.app") == 451
    assert device.version_name("com.app") == "1.2.3"
    assert device.min_sdk("com.app") == 21


def test_version_code_none_when_not_found():
    device, _ = make_device(**{"dumpsys package com.app": "no matching output"})
    assert device.version_code("com.app") is None


def test_file_exists_true_false():
    device, _ = make_device(**{"test -f '/sdcard/x.obb' && echo yes": "yes\n"})
    assert device.file_exists("/sdcard/x.obb") is True

    device2, _ = make_device(**{"test -f '/sdcard/y.obb' && echo yes": ""})
    assert device2.file_exists("/sdcard/y.obb") is False


def test_is_unlocked_checks_dream_state():
    device, _ = make_device(
        **{"dumpsys window": "mShowingDream=false mDreamingLockscreen=false\n"}
    )
    assert device.is_unlocked() is True

    device2, _ = make_device(
        **{"dumpsys window": "mShowingDream=true mDreamingLockscreen=true\n"}
    )
    assert device2.is_unlocked() is False


def test_is_foreground_package():
    device, _ = make_device(
        **{
            "dumpsys window": "  mCurrentFocus=Window{d2b Ru0 com.android.vending/.Foo}\n"
        }
    )
    assert device.is_foreground_package("com.android.vending") is True
    assert device.is_foreground_package("com.whatsapp") is False


def test_focused_window_empty_when_field_absent():
    device, _ = make_device(**{"dumpsys window": "some other unrelated output\n"})
    assert device.focused_window() == ""


def test_dump_ui_clears_then_dumps_then_cats():
    device, adb = make_device(**{"cat /sdcard/window_dump.xml": "<hierarchy/>"})
    result = device.dump_ui()
    assert result == "<hierarchy/>"
    assert adb.shell_log == [
        "rm -f /sdcard/window_dump.xml",
        "uiautomator dump",
        "cat /sdcard/window_dump.xml",
    ]


def test_tap_sends_input_tap_with_coords():
    device, adb = make_device()
    device.tap(123, 456)
    assert "input tap 123 456" in adb.shell_log


def test_get_and_put_setting():
    device, adb = make_device(**{"settings get global stay_on_while_plugged_in": "0\n"})
    assert device.get_setting("global", "stay_on_while_plugged_in") == "0"
    device.put_setting("global", "stay_on_while_plugged_in", "7")
    assert "settings put global stay_on_while_plugged_in 7" in adb.shell_log


def test_get_app_locale_parses_a_set_override():
    device, _adb = make_device(
        **{
            "cmd locale get-app-locales com.android.vending --user 0": (
                "Locales for com.android.vending for user 0 are [fr]"
            )
        }
    )
    assert device.get_app_locale("com.android.vending") == "fr"


def test_get_app_locale_empty_when_unset():
    device, _adb = make_device(
        **{
            "cmd locale get-app-locales com.android.vending --user 0": (
                "Locales for com.android.vending for user 0 are []"
            )
        }
    )
    assert device.get_app_locale("com.android.vending") == ""


def test_set_app_locale_sends_the_locale():
    device, adb = make_device()
    device.set_app_locale("com.android.vending", "en")
    assert (
        "cmd locale set-app-locales com.android.vending --user 0 --locales en"
        in adb.shell_log
    )


def test_force_stop_sends_am_force_stop():
    device, adb = make_device()
    device.force_stop("com.android.vending")
    assert "am force-stop com.android.vending" in adb.shell_log
