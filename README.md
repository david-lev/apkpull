![Alt Text](https://user-images.githubusercontent.com/42866208/149928481-69136427-bad1-4ea3-a6af-0bda0414bf9e.gif)
---
# APKpull

**APKpull provides reliability in downloading apps from Google Play from real Android devices or emulators.**

## How to use?
- Clone APKpull:
```
git clone https://github.com/david-lev/apkpull.git
```
- Go to script directory:
```
cd apkpull
```
- Run the script with package name:
```
./apkpull.sh com.whatsapp
```
- You can also pull the files into spesific path insted of `~/Downloads/apkpull_dl` by adding `-d /path/to/directory` flag, and Uninstall the app after the pulling by adding `--uninstall` flag.  for example:
```
./apkpull.sh com.whatsapp -d ~/Documents/my_apks_dir/ --uninstall
```
- Because each device may have different splits, several devices can be connected at the same time. Only splits that do not already exist locally will be downloaded again.
- The script's exit codes are: `0` if all success, `1`-`9` for every faild device, `10` if pkg not provided, `20` if adb not found, `30` for INT signal (Ctrl+C), `40` if app not in play store, and `50` if no devices connected
- I would recommend following the device screen for the first few times to see how the automation works. Then you can already run it with your eyes closed...
- APKpull uses [ADB](https://developer.android.com/studio/command-line/adb) to connect to devices, jump into the app page in Google Play, make the necessary clicks based on the existing data on the screen, download the app and finally pull it to local as an APK with Splits and even OBB's (if any).
- While running the script, many tests are performed: checking updates, locked screen, device compatibility or country restrictions, paid apps, network, sign in check and more.
- If there is an untreated end case, after several attempts a log will be created that will contain a screenshot and dump ui from the device, which can be attached to a new issue on github.
- Due to the way Google Play app is built, most of the tests and clicks are based on text, so English and Hebrew are currently supported. If you want to support other languages as well, fork this repo, create a list of `buttons_lang` at the beginning of the script, fill in the values in the existing format in the other languages and open PR.
- The files are saved by default to the `~/Downloads/apkpull_dl` folder in the next example format:
```
.
├── com.flambestudios.picplaypost
│   └── 451
│       ├── com.flambestudios.picplaypost_451
│       │   ├── config.arm64_v8a.apk
│       │   ├── config.armeabi_v7a.apk
│       │   ├── config.en.apk
│       │   └── config.xhdpi.apk
│       └── com.flambestudios.picplaypost-451_base.apk
├── com.microsoft.office.officehubrow
│   └── 42787678
│       ├── com.microsoft.office.officehubrow-42787678.apk
│       └── main.42787678.com.microsoft.office.officehubrow.obb
└── tfilon.tfilon
    └── 38
        └── tfilon.tfilon-38.apk
```
