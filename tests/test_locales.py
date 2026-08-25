import dataclasses

from apkpull.locales import LOCALES, get_locale, supported_languages


def test_get_locale_exact_code():
    assert get_locale("en") is LOCALES["en"]


def test_get_locale_normalizes_region_and_case():
    assert get_locale("EN-US") is LOCALES["en"]
    assert get_locale("he-IL") is LOCALES["he"]


def test_get_locale_unsupported_returns_none():
    assert get_locale("de") is None


def test_supported_languages_sorted():
    assert supported_languages() == sorted(LOCALES)


def test_en_has_confirmed_not_found_and_warning_icon_strings():
    assert LOCALES["en"].not_found == "Item not found."
    assert LOCALES["en"].warning_icon == "Warning"


def test_he_has_confirmed_warning_icon_but_not_not_found():
    """Extracted from Google Play's own split_config.iw.apk resources.arsc
    (resource id shared with the English "Warning" entry) and cross-checked:
    it matches the independently hands-on-confirmed country_restricted/
    hardware_incompatible/offline strings already in this ButtonSet.
    not_found isn't a static resource at all (confirmed hands-on: absent from
    base.apk's string table for every locale), so it stays unset here too —
    not a permanent state, just not yet captured live for Hebrew."""
    assert LOCALES["he"].warning_icon == "אזהרה"
    assert LOCALES["he"].not_found is None


def test_es_fr_ru_are_supported_with_all_fields_confirmed_hands_on():
    """es/fr/ru were added via aapt2 resource-id extraction from Google Play's
    own split APKs, cross-checked against the live UI wherever a candidate
    string was ambiguous (multiple resource ids sharing the same English
    text) — resolving two genuine disagreements: French "warning_icon" (candidates
    split "Avertissement"/"Mise en garde" — live screen confirmed
    "Avertissement") and French "sign_in" (candidates split "Se connecter"/
    "Connexion" — live screen confirmed "Connexion")."""
    assert get_locale("es") is LOCALES["es"]
    assert get_locale("fr") is LOCALES["fr"]
    assert get_locale("ru") is LOCALES["ru"]
    assert LOCALES["fr"].warning_icon == "Avertissement"
    assert LOCALES["fr"].sign_in == "Connexion"


def test_no_internet_dialog_confirmed_for_every_locale():
    """A distinct screen from `offline` (confirmed hands-on: "Something went
    wrong" / "No internet connection..." with a "Try again" button, vs.
    `offline`'s themed "You're offline" browsing page) -- both map to the
    same DeviceOfflineError, so every locale needs this filled in too."""
    for lang, buttons in LOCALES.items():
        assert buttons.no_internet_dialog, f"{lang}.no_internet_dialog is unset"


def test_every_locale_defines_all_required_fields_non_empty():
    """Optional fields (a default of ``None``) are for strings only confirmed
    hands-on for some locales so far — those may legitimately be unset."""
    for lang, buttons in LOCALES.items():
        for field in dataclasses.fields(buttons):
            if field.default is None:
                continue
            value = getattr(buttons, field.name)
            assert value, f"{lang}.{field.name} must not be empty"
