![Alt Text](https://camo.githubusercontent.com/de82017ed2ba555b6d64a953576b0af7f89138d1a89ef431bf4fa76932a29f29/68747470733a2f2f692e696d6775722e636f6d2f6f5936627a56682e676966)
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
- I would recommend following the device screen for the first few times to see how the automation works. Then you can already run it with your eyes closed ...

- APKpull uses ADB to connect to devices, jump into the app page in Google Play, make the necessary clicks based on the existing data on the screen, download the app and finally pull it to local as an APK with Splits and even OBB's (if any).
- While running the script, many tests are performed: checking updates, device compatibility or country restrictions, paid apps, network, sign in check and more.
- If there is an untreated end case, after several attempts a log will be created that will contain a screenshot and dump ui from the device, which can be attached to a new issue.
- Due to the way Google Play is built, most of the tests and clicks are based on text, so English and Hebrew are currently supported. If you want to support other languages as well, fork this repo, create a list of buttons_lang at the beginning of the script, fill in the values in the existing format in the other languages and open PR.
