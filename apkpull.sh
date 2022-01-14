#!/bin/bash

###################### APK-PULL | https://github.com/david-lev/apkpull #####################
# apkpull is used for automation for downloading apk's from Android devices and emulators. #
# Usage: apkpull.sh [package-name]; Optionals: [-x or debug] | Created with ❤️ by David Lev #
############################################################################################

e='\e[0m'; r='\e[31m'; g='\e[32m'
y='\e[33m'; b='\e[34m'; p='\e[35m'
echo -e "
$g  ___  ______ _   __            _ _ 
$y / _ \ | ___ \ | / /           | | |
$b/ /_\ \| |_/ / |/ / _ __  _   _| | |
$r|  _  ||  __/|    \| '_ \| | | | | |
$p| | | || |   | |\  \ |_) | |_| | | |
$g\_| |_/\_|   \_| \_/ .__/ \__,_|_|_|
$b                   | |              
$y APK's puller tool $b|_|$y By david-lev$e
"
### VARS & FUNCS ###
trap "echo; echo 'Exiting from apkpull proccess...'; exit 1" INT ERR
[[ "${@}" =~ "-x" ]] && set -x # for debugging
pkg=${1}
as="adb shell"
gp="com.android.vending"
langs="en he"
declare -A buttons_en=( ["cancel"]="Cancel" ["install"]="Install" ["hardware"]="Your device isn't compatible with this version." ["country"]="This item isn't available in your country." )
declare -A buttons_he=( ["cancel"]="ביטול" ["install"]="התקנה" ["hardware"]="המכשיר שלך אינו תואם לגירסה זו." ["country"]="פריט זה אינו זמין בארצך." )
shopt -s extglob
tmp_file=$(mktemp)
function split() { aapt d badging ${1} 2>/dev/null | grep -Po "split='\K[^']*"; }
function error() { >&2 echo -e "${r}ERROR: ${1}${2}${e}"; [[ ${3} =~ ^[0-9]{1,3}$ && ${3} -ge 0 && ${3} -le 255 ]] && exit ${3}; }
function print() { echo -e ">> ${1}${2}${e}"; }
function is_device_connected() { [[ $(adb ${device_id} get-state 2>/dev/null) == "device" ]]; }
function is_still_connected() { is_device_connected || error ${r} "Device ${y}${device_model}${r} disconnected!" 1; };
function is_installed() { ${as} pm path ${1} &>/dev/null; }
function is_disabled() { ${as} pm list packages -d | grep -w -q ${1} &>/dev/null; }
function is_unlocked() { ${as} dumpsys window 2>/dev/null | grep -w -q "mShowingDream=false mDreamingLockscreen=false"; }
function get_button_coords() {
    ${as} uiautomator dump &>/dev/null
    ui=$(${as} cat /sdcard/window_dump.xml 2>/dev/null | tee ${tmp_file})
    coords=$(perl -ne 'printf "%d %d\n", ($1+$3)/2, ($2+$4)/2 if /text="'${1}'"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/' <<<${ui} 2>/dev/null)
    [[ -z "${coords}" ]] && return 1 || echo ${coords}
}

### CHECKS ###
if [[ ${#} < 1 ]]; then
    error ${r} "Package name must be provided! ${y}apkpull [PACKAGE NAME]${r}, Optionals: ${y}[--ignore-missing-arms] [-x]" 1
elif [[ $(curl --connect-timeout 0.5 -s -o /dev/null -w "%{http_code}" "https://play.google.com/store/apps/details?id=${pkg}") == 404 ]]; then
    error ${r} "This app doesn't exists in Google Play." 1
elif ! command -v adb >/dev/null 2>&1; then
    error ${r} "Unable to find ADB, please install or add to PATH." 1
elif ! is_device_connected; then
    error ${r} "No device found / more than one device connected!" 1
elif ! is_installed ${gp}; then
    error ${r} "Google Play is not installed on the connected device!" 1
elif is_disabled ${gp}; then
    error ${r} "Google Play app is disabled!" 1
else
    device_id="-s $(${as} getprop ro.boot.serialno)"
    device_model=$(${as} getprop ro.product.model)
    device_abi=$(${as} getprop ro.product.cpu.abi)
    device_lang=$(${as} getprop persist.sys.locale); declare -n buttons="buttons_${device_lang:0:2}"
    print ${g} "Device ${y}${device_model}${g} with ${y}${device_abi}${g} processor is successfully connected!"
    as="adb ${device_id} shell"
fi

### DOWNLOAD ###
if ! is_installed ${pkg}; then
    if ! is_unlocked; then
       print ${r} "The device is locked. Unlocked it to continue..."
        while ! is_unlocked; do
            is_still_connected
            sleep 1s
        done
        print ${g} "Device unlocked! Starting to download..."
    fi
    print ${g} "Launching Google Play to ${y}${pkg}${g} app page."
    ${as} am start -a android.intent.action.VIEW -d "https://play.google.com/store/apps/details?id=${pkg}" -p ${gp} &>/dev/null
    if grep -w -q ${device_lang:0:2} <<< ${langs}; then
        while ! install_coords=$(get_button_coords ${buttons["install"]}); do
            grep "${buttons["hardware"]}" -w -q ${tmp_file} && error ${r} "${buttons_en["hardware"]}" 3
            grep "${buttons["country"]}" -w -q ${tmp_file} && error ${r} "${buttons_en["country"]}" 3
        done
        while ! get_button_coords ${buttons["cancel"]} &>/dev/null; do
            ${as} input tap ${install_coords}
            is_still_connected
        done
        print ${g} "The download has started, check the device to see the progress..."
    else
        print ${y} "The device language ${g}(${device_lang:0:2})${y} is not supported by the script, you need to install the app manually."
    fi
    while ! is_installed ${pkg}; do
        is_still_connected
    done
    print ${g} "The ${y}${pkg}${g} package successfully installed!"
fi

### PULL ###
vc=$(${as} pm list packages --show-versioncode | grep -w ${pkg} | sed 's/.*versionCode://g')
dl="/home/${USER}/Downloads/apkpull_dl/${pkg}/${vc}"
mkdir -p ${dl}/tmp
is_still_connected
if apk_paths=$(${as} pm path ${pkg} | sed 's/package://g'); then
    print ${g} "Pulling $(echo ${apk_paths} | sed 's/\s/\n/g' | wc -l) files from the device..."
    for apk in ${apk_paths}; do
        adb ${device_id} pull ${apk} ${dl}/tmp
    done
else
    error ${r} "Unable to get paths for the apk files :(" 2
fi

cd ${dl} || error ${r} "Can't go into ${dl}" 20
print ${y} "Checking & renaming files..."
for split in tmp/*; do
    if [[ ${split} == tmp/split* ]]; then
        new_name=$(basename ${split} | sed 's/split_//')
        mv -f ${split} ${new_name}
    else
        base="${pkg}-${vc}_base.apk"
        (test -f ${base} || test -f ${base/_base/}) || mv -f ${split} ${base}
    fi
done
rm -rf tmp

if [[ $(ls -1 | wc -l) > 1 ]]; then # if there are split inside the folder
    folder="${pkg}_${vc}"
    mkdir -p ${folder}
    mv -f !(${pkg}*) ${folder}
    msg="There are $(ls -1 ${folder} | wc -l) splits and one base apk file"
else # if there is only one apk file
    test -f ${base} && mv ${base} "${base/_base/}"; base=${base/_base/} # remove "_base" from apk filename
fi

print ${g} "The operation was completed successfully!. ${msg}"
