import re
import subprocess
import multiprocessing
from typing import Optional, Iterable, Tuple, Union
from dataclasses import dataclass

GOOGLE_PLAY_PKG = "com.android.vending"
MAX_RETRIES = 3
SUPPORTED_LANGS = ["en", "he"]
COINS = ["$", "₪"]
BUTTONS = {
    "en": {
        "open": "Open",
        "play": "Play",
        "install": "Install",
        "uninstall": "Uninstall",
        "enable": "Enable",
        "update": "Update",
        "cancel": "Cancel",
        "accept": "Accept",
        "sign_in": "Sign in",
        "installing": "Installing...",
        "pending": "Pending...",
        "of": "of",
        "hardware": "Your device isn't compatible with this version.",
        "country": "This item isn't available in your country.",
        "network": "You're offline"
    },
    "he": {
        "open": "פתח",
        "play": "שחק",
        "install": "התקנה",
        "uninstall": "הסר התקנה",
        "enable": "הפעלה",
        "update": "עדכון",
        "cancel": "ביטול",
        "accept": "אישור",
        "sign_in": "כניסה",
        "installing": "מתקין...",
        "pending": "בהמתנה...",
        "of": "מתוך",
        "hardware": "המכשיר שלך אינו תואם לגירסה זו.",
        "country": "פריט זה אינו זמין בארצך.",
        "network": "אין חיבור לאינטרנט"
    }
}


def run_command(*args, **kwargs) -> str:
    """
    Runs a command and returns the output.

    Args:
        args: The arguments to pass to the command.
        kwargs: The keyword arguments to pass to :func:`subprocess.run`.

    Returns:
        The output of the command.
    """
    return subprocess.run(args,
                          stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE,
                          check=True,
                          **kwargs).stdout.decode('utf-8').strip()


def run_adb_command(device_id: str, *args, adb_path: Optional[str] = None) -> str:
    """
    Runs an adb command on a device.

    Args:
        adb_path: The path to the adb executable (If not specified, adb will be searched in the ``PATH``).
        device_id: The id of the device to run the command on.
        args: The arguments to pass to adb.
    """
    return run_command(adb_path, '-s', device_id, *args)


class NotDownloadable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class Bounds:
    left: int
    top: int
    right: int
    bottom: int

    @classmethod
    def from_automator_dump(cls, dump: str, button: str) -> Optional['Bounds']:
        pattern = fr'<node .* text="{re.escape(button)}" .* bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"'
        match = re.search(pattern, dump)
        if match:
            return Bounds(*map(int, match.groups()))
        return None


@dataclass(frozen=True, slots=True)
class Package:
    name: str
    version_code: int
    version_name: str
    paths: Tuple[str, ...]
    min_sdk: int

    @classmethod
    def from_dumpsys(cls, package_name: str, paths: Iterable[str], output: str) -> 'Package':
        return cls(
            name=package_name,
            version_code=int(re.search(r'versionCode=(\d+)', output).group(1)),
            version_name=re.search(r'versionName=(\S+)', output).group(1),
            paths=tuple(paths),
            min_sdk=int(re.search(r'minSdk=(\d+)', output).group(1))
        )


@dataclass(frozen=True, slots=True)
class Device:
    """
    A class representing an Android device.
    """
    id: str
    model: str
    abi: str
    lang: str
    sdk: int
    stay_on_while_plugged_in: int

    @classmethod
    def from_getprop(cls, output: str, stay_on_while_plugged_in: int) -> 'Device':
        """
        Create a device from the output of ``adb shell getprop``.

        Args:
            output: The output of ``adb shell getprop``.
            stay_on_while_plugged_in: Whether to keep the device awake while plugged in.

        Returns:
            A device object.
        """
        props = dict(line.split(':', 1) for line in output.strip().split('\n'))
        return cls(id=props['[ro.serialno]'],
                   model=props['[ro.product.model]'],
                   abi=props['[ro.product.cpu.abi]'],
                   lang=props['[persist.sys.locale]'].split('-')[0],
                   sdk=int(props['[ro.build.version.sdk]']),
                   stay_on_while_plugged_in=stay_on_while_plugged_in)

    def execute(self, *args: str) -> str:
        """
        Execute a command on the device.

        Args:
            args: The arguments to pass to the command.

        Returns:
            The output of the command.
        """
        return run_adb_command(self.id, 'shell', *args)

    def getprop(self, prop: Optional[str]) -> str:
        """
        Get a property of the device.

        Args:
            prop: The property to get.

        Returns:
            The value of the property.
        """
        return self.execute('getprop', prop) if prop else self.execute('getprop')

    def setprop(self, prop: str, value: str) -> None:
        """
        Set a property of the device.

        Args:
            prop: The property to set.
            value: The value to set.
        """
        self.execute('setprop', prop, value)

    def settings(self, action: str, settings: str, key: str, value: Optional[Union[str, int]] = None) -> Optional[str]:
        """
        Change or get a setting.

        Args:
            action: The action to perform (get, put, list).
            settings: The settings to change (system, secure, global).
            key: The key to change.
            value: The value to set.
        """
        return self.execute('shell', 'settings', action, settings, key, (str(value) if value is not None else ''))

    def is_still_connected(self) -> bool:
        """
        Check if the device is still connected.

        Returns:
            Whether the device is still connected.
        """
        return self.id in run_command('adb', 'devices')

    def get_package_paths(self, pkg: str) -> Tuple[str, ...]:
        """
        Get the path of a package on the device.

        Args:
            pkg: The package name.

        Returns:
            A tuple of paths.

        Raises:
            CalledProcessError: If the package is not installed.
        """
        return tuple(line.split(':', 1)[-1].strip() for line in
                     self.execute('shell', 'pm', 'path', pkg).split('\n'))

    def get_package(self, pkg: str) -> Package:
        """
        Get the information of a package on the device.

        Args:
            pkg: The package name.

        Returns:
            A package object.
        """
        return Package.from_dumpsys(pkg, self.get_package_paths(pkg), self.execute('shell', 'dumpsys', 'package', pkg))

    def is_package_installed(self, pkg: str) -> bool:
        """
        Check if a package is installed on the device.

        Args:
            pkg: The package name.
        """
        try:
            self.get_package_paths(pkg)
            return True
        except subprocess.CalledProcessError:
            return False

    def is_package_disable(self, pkg: str) -> bool:
        """
        Check if a package is disabled on the device.

        Args:
            pkg: The package name.
        """
        return f'package:{pkg}' in self.execute('shell', 'pm', 'list', 'packages', '-d', pkg).split('\n')

    def is_unlocked(self) -> bool:
        """
        Check if the device is unlocked.
        """
        return 'mShowingDream=false mDreamingLockscreen=false' in self.execute('shell', 'dumpsys', 'window')

    def is_on_google_play(self) -> bool:
        """
        Check if the device is on Google Play.
        """
        return GOOGLE_PLAY_PKG in self.execute('shell', 'dumpsys', 'activity', 'activities', '|', 'grep',
                                               'mResumedActivity')

    def launch_package_screen_on_google_play(self, pkg: str):
        """
        Launch a package screen on Google Play.

        Args:
            pkg: The package name.
        """
        self.execute('shell', 'am', 'start', '-a', 'android.intent.action.VIEW', '-d',
                     f'market://details?id={pkg}', '-p', GOOGLE_PLAY_PKG)

    def set_stay_on_while_plugged_in(self, status: int):
        """
        Set the ``stay_on_while_plugged_in`` setting.
        """
        self.settings(action='put', settings='global', key='stay_on_while_plugged_in', value=status)

    def restore_stay_on_while_plugged_in(self):
        """
        Restore the ``stay_on_while_plugged_in`` setting.
        """
        self.set_stay_on_while_plugged_in(self.stay_on_while_plugged_in)

    def get_screen_xml(self, force_new_dump: bool) -> str:
        """
        Get the screen XML.

        Args:
            force_new_dump: Whether to force a new dump.
        """
        if force_new_dump:
            self.execute('shell', 'rm', '-f', '/sdcard/window_dump.xml')
            self.execute('shell', 'uiautomator', 'dump')
        return self.execute('cat', '/sdcard/window_dump.xml')

    def get_button_coords(self, button_id: str, force_new_dump: bool) -> Optional[Bounds]:
        """
        Get the coordinates of a button on the screen.

        Args:
            button_id: The id of the button.
            force_new_dump: Whether to force a new dump.
        Returns:
            The coordinates of the button.
        """
        return Bounds.from_automator_dump(self.get_screen_xml(force_new_dump), BUTTONS[self.lang][button_id])

    def show_progress(self):
        text = self.get_screen_xml(force_new_dump=True)
        # Extract the progress percentage from the text
        match = re.search(r'text="(\d+%)', text)
        if match:
            progress = match.group(1)
            text = f'Downloading {progress}...'
        else:
            # Check if the "pending" button is present in the text
            if BUTTONS[self.lang]['pending'] in text:
                text = 'Pending...'
            # Check if the "installing" button is present in the text
            elif BUTTONS[self.lang]['installing'] in text:
                text = 'Installing...'
        print(f">> {self.model}: \033[33m{text}\033[0m\033[0K", end='\r')

    def click(self, bounds: Bounds):
        """
        Click on the screen.

        Args:
            bounds: The bounds of the button.
        """
        self.execute('shell', 'input', 'tap', *map(str, (bounds.left, bounds.top, bounds.right, bounds.bottom)))

    def pull(self, src: str, dest: str):
        """
        Pull a file from the device.

        Args:
            src: The source path on the device.
            dest: The destination path on the host.
        """
        self.execute('pull', src, dest)

    def __enter__(self):
        self.set_stay_on_while_plugged_in(status=7)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.restore_stay_on_while_plugged_in()


def get_connected_devices(adb_path: Optional[str] = None) -> Tuple[str]:
    return tuple(
        line.split("\t")[0]
        for line in run_command(adb_path, 'devices').split("\n")[1:]
        if line.endswith('device')
    )


def get_devices(devices: Iterable[str]) -> Tuple[Device]:
    """
    Returns a tuple of all connected devices.
    """
    return tuple(
        Device.from_getprop(output=run_adb_command(device, 'shell', 'getprop'),
                            stay_on_while_plugged_in=int(run_adb_command(device, 'shell', 'settings', 'get', 'global',
                                                                         'stay_on_while_plugged_in'))) for device in
        devices)


def download_app(device: Device, pkg: str):
    while not device.get_button_coords('open', force_new_dump=True):
        while not device.is_package_installed(pkg):
            retries = 0
            while True and retries < MAX_RETRIES:
                install_cords = device.get_button_coords('install', force_new_dump=True)
                if install_cords is None:
                    screen = device.get_screen_xml(force_new_dump=False)
                    if BUTTONS[device.lang]['hardware'] in screen:
                        raise NotDownloadable(f'App {pkg} is not downloadable on {device.model}')
                    elif BUTTONS[device.lang]['country'] in screen:
                        raise NotDownloadable(f'App {pkg} is not available in {device.lang} on {device.model}')
                    elif BUTTONS[device.lang]['network'] in screen:
                        raise NotDownloadable(f'App {pkg} is not downloadable on {device.model} over the current network')
                    elif BUTTONS[device.lang]['sign_in'] in screen:
                        raise NotDownloadable(f'App {pkg} requires sign in on {device.model}')
                    elif any(coin in screen for coin in COINS):
                        raise NotDownloadable(f'App {pkg} is paid on {device.model}')
                else:
                    break
                retries += 1
            else:
                raise NotDownloadable(f'Unknown error while downloading app {pkg} on {device.model}')

            install_retries = 0
            device.click(install_cords)
            while not BUTTONS[device.lang]['cancel'] in device.get_screen_xml(force_new_dump=True) \
                    and install_retries < MAX_RETRIES:
                install_retries += 1

        else:
            retries = 0
            while True and retries < MAX_RETRIES:
                open_cords = device.get_button_coords('open', force_new_dump=True)
                while not device.get_button_coords('open', force_new_dump=True):
                old_version = device.get_package(pkg).version_code
                while device.get_package(pkg).version_code == old_version:
                    u

