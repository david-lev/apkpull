"""Google Play Store button/text strings, keyed by 2-letter language code.

Play Store's UI exposes no stable resource-ids on its buttons (verified by
dumping the live UI tree with uiautomator — every text node's ``resource-id``
is empty), so text matching is the only reliable way to drive it. This table
is the single place that knowledge lives; adding a language is a data change
here, not a code change anywhere else.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ButtonSet:
    open: str
    play: str
    install: str
    uninstall: str
    deactivate: str
    update: str
    cancel: str
    hardware_incompatible: str
    country_restricted: str
    offline: str
    sign_in: str | None = None
    """Play Store's "Sign in" prompt on the details page when no Google account
    is signed in. Unlike the tap-target fields above, this is only ever used
    for error *detection* (never tapped, never an ``also_break_on`` match), so
    — like ``not_found``/``warning_icon``/``insufficient_storage`` below — a
    locale can be fully functional without it confirmed yet. ``None`` for
    locales where this hasn't been confirmed hands-on."""
    not_found: str | None = None
    """The dedicated "Item not found." page shown for a package with no Play Store
    listing at all — a distinct full-page state, not a warning banner on the
    details page. ``None`` for locales where this hasn't been confirmed hands-on."""
    warning_icon: str | None = None
    """``content-desc`` of the red warning-triangle icon Play Store shows next to a
    banner-style error message on the details page — confirmed hands-on for the
    region-restricted screen; not verified for every other banner. Lets
    :func:`apkpull.automation.PlayStoreAutomator._check_error_screens` catch and
    surface banner errors it doesn't have a specific rule for, instead of silently
    polling until :class:`~apkpull.exceptions.AutomationTimeoutError` gives up with
    no real explanation. ``None`` for locales where this hasn't been confirmed
    hands-on."""
    insufficient_storage: str | None = None
    """Title of the "Not enough storage" dialog Play Store shows when there isn't
    room on the device to install/update — confirmed hands-on (reproduced against
    a genuinely low-storage emulator) and, unlike ``not_found``, a real static
    resource: extracted for every locale here directly from Play Store's own
    resources.arsc. A plain ``AlertDialog``, not paired with ``warning_icon``, so
    it needs its own field to be caught at all. ``None`` for locales where this
    hasn't been confirmed hands-on."""
    no_internet_dialog: str | None = None
    """Body text of the full-page "Something went wrong" / "No internet
    connection..." state — confirmed hands-on, and a *different* screen from
    ``offline`` (that one's the themed "You're offline" browsing page; this one
    is a plainer error page with a "Try again" button, seen after tapping
    something while offline rather than just landing on an offline page).
    Matched on the body text rather than the "Something went wrong" title,
    which is reused by unrelated errors elsewhere in Play Store (multiple
    resource ids). Raises the same :class:`~apkpull.exceptions.DeviceOfflineError`
    as ``offline`` — same underlying condition, just a different rendering.
    ``None`` for locales where this hasn't been confirmed hands-on."""


LOCALES: dict[str, ButtonSet] = {
    "en": ButtonSet(
        open="Open",
        play="Play",
        install="Install",
        uninstall="Uninstall",
        deactivate="Deactivate",
        update="Update",
        cancel="Cancel",
        hardware_incompatible="Your device isn't compatible with this version.",
        country_restricted="This item isn't available in your country.",
        offline="You're offline",
        sign_in="Sign in",
        not_found="Item not found.",
        warning_icon="Warning",
        insufficient_storage="Not enough storage",
        no_internet_dialog="No internet connection. Make sure Wi‑Fi or cellular data is turned on, then try again.",
    ),
    "he": ButtonSet(
        open="פתח",
        play="שחק",
        install="התקנה",
        uninstall="הסר התקנה",
        deactivate="ביטול הפעלה",
        update="עדכון",
        cancel="ביטול",
        hardware_incompatible="המכשיר שלך אינו תואם לגירסה זו.",
        country_restricted="פריט זה אינו זמין בארצך.",
        offline="אין חיבור לאינטרנט",
        sign_in="כניסה",
        warning_icon="אזהרה",
        insufficient_storage="אין מספיק נפח אחסון פנוי",
        no_internet_dialog="אין חיבור לאינטרנט. יש לוודא שה-Wi-Fi או חבילת הגלישה מופעלים ולנסות שוב.",
    ),
    "es": ButtonSet(
        open="Abrir",
        play="Jugar",
        install="Instalar",
        uninstall="Desinstalar",
        deactivate="Desactivar",
        update="Actualizar",
        cancel="Cancelar",
        hardware_incompatible="Tu dispositivo no es compatible con esta versión.",
        country_restricted="Este elemento no está disponible en tu país.",
        offline="No tienes conexión",
        sign_in="Iniciar sesión",
        not_found="Elemento no encontrado",
        warning_icon="Advertencia",
        insufficient_storage="No hay suficiente almacenamiento",
        no_internet_dialog="Sin conexión a Internet. Comprueba que el Wi‑Fi o los datos están activados y vuelve a intentarlo.",
    ),
    "fr": ButtonSet(
        open="Ouvrir",
        play="Jouer",
        install="Installer",
        uninstall="Désinstaller",
        deactivate="Désactiver",
        update="Mettre à jour",
        cancel="Annuler",
        hardware_incompatible="Votre appareil n'est pas compatible avec cette version.",
        country_restricted="Cet article n'est pas disponible dans votre pays.",
        offline="Vous êtes hors connexion",
        sign_in="Connexion",
        not_found="Élément introuvable.",
        warning_icon="Avertissement",
        insufficient_storage="Espace de stockage insuffisant",
        no_internet_dialog="Aucune connexion Internet. Assurez-vous que le Wi-Fi/les données mobiles sont activés, et réessayez.",
    ),
    "ru": ButtonSet(
        open="Открыть",
        play="Играть",
        install="Установить",
        uninstall="Удалить",
        deactivate="Отказаться",
        update="Обновить",
        cancel="Отмена",
        hardware_incompatible="Не поддерживается на вашем устройстве.",
        country_restricted="Недоступно в вашей стране.",
        offline="Вы не в Сети",
        sign_in="Войти",
        not_found="Файл не найден.",
        warning_icon="Предупреждение",
        insufficient_storage="Недостаточно места",
        no_internet_dialog="Нет доступа к Интернету. Проверьте подключение к Wi-Fi или сотовой сети и повторите попытку.",
    ),
}

PAID_APP_CURRENCY_SYMBOLS = ("₪", "$", "€", "£", "₽", "¥", "₹")


def get_locale(lang: str) -> ButtonSet | None:
    """Look up a :class:`ButtonSet` by a ``persist.sys.locale``-style tag (e.g. ``en-US``)."""
    return LOCALES.get(lang[:2].lower())


def supported_languages() -> list[str]:
    return sorted(LOCALES)
