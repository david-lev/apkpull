#!/bin/bash

############################### APK-PULL | https://github.com/david-lev/apkpull ###############################
# APKpull is used to create automations for downloading android apps from Google Play and puling them to a -  #
# - local machine or server as apk files. You can connect several devices at the same time.                   #
# Usage: apkpull.sh [package-name]; Optionals: [-d /path/to/dir] [--uninstall] | Created with ❤️ by David Lev  #
###############################################################################################################


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
trap "echo; echo 'Exiting from apkpull proccess...'; exit 30" INT
[[ "${@}" =~ "-x" ]] && set -x # for debugging
[[ ${2} == "-d" && -d "${3}" ]] && dl_dir="${3}" || dl_dir="${HOME}/Downloads"
pkg="${1}"
logs_dir="/tmp/apkpull_log"
gp="com.android.vending"
langs="en he"
coins="₪|$"
max_rounds=5
declare -A buttons_en=( ["open"]="Open" ["play"]="Play" ["install"]="Install" ["uninstall"]="Uninstall" ["deactivate"]="Deactivate" ["update"]="Update" ["cancel"]="Cancel" ["accept"]="Accept" ["sign_in"]="Sign in" ["installing"]="Installing..." ["pending"]="Pending..." ["of"]="of" ["hardware"]="Your device isn't compatible with this version." ["country"]="This item isn't available in your country." ["network"]="You're offline" )
declare -A buttons_he=( ["open"]="פתח" ["play"]="שחק" ["install"]="התקנה" ["uninstall"]="הסר התקנה" ["deactivate"]="ביטול הפעלה" ["update"]="עדכן" ["cancel"]="ביטול" ["accept"]="אישור" ["sign_in"]="כניסה" ["installing"]="מתקין..." ["pending"]="בהמתנה..." ["of"]="מתוך" ["hardware"]="המכשיר שלך אינו תואם לגירסה זו." ["country"]="פריט זה אינו זמין בארצך." ["network"]="אין חיבור לאינטרנט" )
tmp_file=$(mktemp)
function print() { echo -e ">> ${device_model:-APKPULL}: ${1}${2}${e}"; [[ ${3} =~ ^[0-9]{1,3}$ && ${3} -ge 0 && ${3} -le 255 ]] && exit ${3} || true; }
function is_device_connected() { [[ "$(adb -s ${device_id} get-state 2>/dev/null)" == "device" ]]; }
function is_still_connected() { is_device_connected || (print ${r} "Device ${y}${device_model}${r} disconnected!" && false); };
function is_installed() { ${as} pm path ${1} &>/dev/null; }
function is_disabled() { ${as} pm list packages -d | grep -wq ${1} &>/dev/null; }
function is_on_gplay() { [[ "$(${as} dumpsys activity activities | grep mResumedActivity)" =~ ${gp} ]]; }
function launch() { ${as} am start -a android.intent.action.VIEW -d "https://play.google.com/store/apps/details?id=${pkg}" -p ${gp} &>/dev/null; }
function is_unlocked() { ${as} dumpsys window 2>/dev/null | grep -wq "mShowingDream=false mDreamingLockscreen=false"; }
function restore_stay_on() { ${as} settings put global stay_on_while_plugged_in ${stay_on_status:-0}; }
function get_button_coords() {
    ${as} rm -f /sdcard/window_dump.xml
    ${as} uiautomator dump &>/dev/null
    ${as} cat /sdcard/window_dump.xml > ${tmp_file} 2>/dev/null
    coords=$(perl -ne 'printf "%d %d\n", ($1+$3)/2, ($2+$4)/2 if /text="'${1}'"[^>]*bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"/' ${tmp_file} 2>/dev/null)
    [[ -z "${coords}" ]] && return 1 || echo ${coords}
}
function show_progress() {
    text=$(sed -n "s/.*text=\"\([0-9]\+%\).*/Downloading \1.../p" ${tmp_file})
    grep -q ${buttons["pending"]} ${tmp_file} && text="Pending..."; grep -q ${buttons["installing"]} ${tmp_file} && text="Installing..."
    echo -ne ">> ${device_model}: \e[33m${text}\e[0m\033[0K\r"
}
function capture_error() {
    mkdir -p ${logs_dir}
    log_name="${logs_dir}/$(date +'%d.%m.%y_%T')_${device_model}_${device_lang}"
    cp ${tmp_file} "${log_name}.xml"
    adb -s ${device_id} exec-out screencap -p > "${log_name}.png"
    print ${y} "LOG: Screenshot saved as XML file and PNG to ${g}${log_name}.png ${log_name}.xml"
}
function usage() {
    echo "Usage: apkpull.sh [PACKAGE] [OPTIONS]"
    echo "   example: apkpull.sh com.whatsapp -d ~/Documents/my_apks/ --uninstall"
    echo "  -h, --help              display this help and exit"
    echo "  --uninstall             uninstall the app after pulling"
    echo "  -d path to directory    pull the files into spesific path insted of ~/Downloads/apkpull_dl/"
    echo -e "\nFor bug reports, questions, issues: https://github.com/david-lev/apkpull"
}

### CHECKS ###
if [[ "${1}" == "--help" || ${1} == "-h" ]]; then
    usage && exit 10
elif [[ "${1}" == -* ]]; then
    print ${r} "Unknown command. Run ${y}${0} --help${r} for more info." 10
elif [[ $# -lt 1 ]]; then
    print ${r} "Package name must be provided! Run ${y}${0} --help${r} for more info." 10
elif ! (grep -Pq "^([A-Za-z]{1}[A-Za-z\d_]*\.)+[A-Za-z][A-Za-z\d_]*$"<<<${pkg}); then
    print ${r} "Invalid syntax for package name." 40
elif [[ $(curl --connect-timeout 0.5 -s -o /dev/null -w "%{http_code}" "https://play.google.com/store/apps/details?id=${pkg}") == 404 ]]; then
    print ${r} "This app doesn't exists in Google Play." 40
elif ! command -v adb >/dev/null 2>&1; then
    print ${r} "Unable to find ADB, please install or add to PATH." 20
elif [[ $(adb devices -l | sed '/List.*\|^$/d; s/\s.*//g' | wc -l) -lt 1 ]]; then
    print ${r} "No devices found! At least one device must be connected" 50
else
    devices=$(adb devices -l | sed '/List.*\|^$/d; s/\s.*//g')
    print ${g} "$(echo ${devices} | sed 's/\s/\n/g' | wc -l) devices connected!"
fi


### LOOP DEVICES ###
actions=0; successful_actions=0
for device_id in ${devices}; do
    : $((actions++))
    if ! is_device_connected; then
        print ${r} "The device is $(adb -s ${device_id} get-state 2>&1| head -n 1 | sed 's/error: device \|\.//g')!" && continue
    else
        as="adb -s ${device_id} shell"
        device_model=$(${as} getprop ro.product.model)
        device_abi=$(${as} getprop ro.product.cpu.abi)
        device_lang=$(${as} getprop persist.sys.locale)
        declare -n buttons="buttons_${device_lang:0:2}"
        stay_on_status=$(${as} settings get global stay_on_while_plugged_in)
        if ! is_installed ${gp} || is_disabled ${gp}; then
            print ${r} "Google Play is disabled or not installed in ${device_model}!" && continue
        else
            print ${g} "Device ${y}${device_model}${g} with ${y}${device_abi}${g} processor is successfully connected!"
        fi
        if ! is_unlocked; then
            print ${r} "The device is locked. Unlocked it to continue..."
            while ! is_unlocked; do
                is_still_connected || continue 2
            done
            print ${g} "Device unlocked!"
        fi
        ${as} settings put global stay_on_while_plugged_in 7

        ### DOWNLOAD ###
        print ${g} "Launching Google Play to ${y}${pkg}${g} app page." && launch
        if ! is_installed ${pkg}; then
            if grep -wq ${device_lang:0:2} <<< ${langs}; then
                while ! install_coords=$(get_button_coords ${buttons["install"]}); do
                    grep -wq "${buttons["hardware"]}" ${tmp_file} && print ${r} "${buttons_en["hardware"]}" && continue 2
                    grep -wq "${buttons["country"]}" ${tmp_file} && print ${r} "${buttons_en["country"]}" && continue 2
                    grep -wq "${buttons["network"]}" ${tmp_file} && print ${r} "${buttons_en["network"]}." && continue 2
                    grep -Ewq "[0-9]+\.[0-9]+.(${coins})" ${tmp_file} && print ${r} "This app is paid." && continue 2
                    grep -wq "\"${buttons["sign_in"]}\"" ${tmp_file} && print ${r} "You must be logged in to a Google account." && continue 2
                    grep -wq "\"${buttons["cancel"]}\"" ${tmp_file} && print ${y} "The app is already in the download process." && install_coords="skip" && break
                    is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                    is_still_connected || continue 2
                    : $((install_rounds++))
                    if  [[ ${install_rounds} -ge ${max_rounds} ]]; then
                        print ${r} "An unknown error occurred."
                        capture_error && continue 2
                    fi
                done
                if [[ ${install_coords} != "skip" ]]; then
                    while ! get_button_coords ${buttons["cancel"]} &>/dev/null; do
                        ${as} input tap ${install_coords}
                        accept_coords=$(get_button_coords ${buttons["accept"]}) && ${as} input tap ${accept_coords} && print ${y} "Permissions approved."
                        is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                        is_still_connected || continue 2
                    done
                    print ${g} "The download has started..."
                fi
            else
                print ${y} "The device language ${g}(${device_lang:0:2})${y} is not supported by apkpull, you need to install the app manually."
            fi
            while ! is_installed ${pkg}; do
                show_progress
                is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                is_still_connected || continue 2
                install_coords=$(get_button_coords ${buttons["install"]}) && ${as} input tap ${install_coords} && print ${y} "Download canceled manually, installs again."
                accept_coords=$(get_button_coords ${buttons["accept"]}) && ${as} input tap ${accept_coords} && print ${y} "Permissions approved."
            done
            print ${g} "The ${y}${pkg}${g} package successfully installed!"
        else
            if grep -w -q ${device_lang:0:2} <<< ${langs}; then
                while ! update_coords=$(get_button_coords ${buttons["update"]}); do
                    grep -wq "\"${buttons["network"]}\"" ${tmp_file} && print ${r} "${buttons_en["network"]}, Can't check for updates." && break
                    grep -wq "\"${buttons["sign_in"]}\"" ${tmp_file} && print ${r} "You must be logged in to a Google account, Can't check for updates." && break
                    grep -wq "\"${buttons["cancel"]}\"" ${tmp_file} && print ${y} "The app is already in the update process." && update_coords="skip" && break
                    grep -Ewq "(\"${buttons["open"]}\"|\"${buttons["play"]}\")" ${tmp_file} && break
                    grep -Ewq "(\"${buttons["uninstall"]}\"|\"${buttons["deactivate"]}\")" ${tmp_file} && ! grep -Ewq "(\"${buttons["open"]}\"|\"${buttons["play"]}\")" ${tmp_file} && break
                    is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                    : $((update_rounds++))
                    if  [[ ${update_rounds} -ge ${max_rounds} ]]; then
                        print ${r} "An unknown error occurred."
                        capture_error && print ${r} "Can't check for updates." && break
                    fi
                done
                if [[ -n ${update_coords} ]]; then
                    if [[ ${update_coords} != "skip" ]]; then
                        while ! get_button_coords ${buttons["cancel"]} &>/dev/null; do
                            ${as} input tap ${update_coords}
                            is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                            is_still_connected || continue 2
                        done
                        print ${g} "The update has started..."
                    fi
                    current_vcode=$(${as} pm list packages --show-versioncode | grep "package:${pkg} " | sed 's/.*versionCode://g')
                    while [[ ${current_vcode} == $(${as} pm list packages --show-versioncode | grep "package:${pkg} " | sed 's/.*versionCode://g') ]]; do
                        show_progress
                        is_on_gplay || (print ${y} "Device exited from Google Play, Launching again.." && launch)
                        is_still_connected || continue 2
                        update_coords=$(get_button_coords ${buttons["update"]}) && ${as} input tap ${update_coords} && print ${y} "Update canceled manually, installs again."
                    done
                    print ${g} "The ${y}${pkg}${g} package successfully updated!"
                else
                    print ${g} "The ${y}${pkg}${g} package already installled and updated."
                fi
            else
                print ${y} "The device language ${g}(${device_lang:0:2})${y} is not supported by apkpull to check for updates."
            fi 
        fi

        ### PULL ###
        base_pulled=0; splits_pulled=0; obbs_pulled=0
        vcode=$(${as} pm list packages --show-versioncode | grep "package:${pkg} " | sed 's/.*versionCode://g')
        dl="${dl_dir}/apkpull_dl/${pkg}/${vcode}"
        mkdir -p "${dl}"
        is_still_connected || continue
        base="${pkg}-${vcode}_base.apk"
        if paths=$(${as} pm path ${pkg} | sed 's/package://g'); then
            unset apk_paths
            for _path in ${paths}; do apk_paths+=("${_path}"); done
            for apk_path in ${apk_paths[@]}; do
                if [[ ${#apk_paths[@]} == 1 ]]; then
                    if ! test -f "${dl}/${base/_base}"; then
                        print ${g} "Pulling ${y}${base/_base} ($(${as} du -sh ${apk_path} | sed 's/\s.*//g'))${g}..."
                        adb -s ${device_id} pull ${apk_path} "${dl}/${base/_base}" >/dev/null && : $((base_pulled++))
                    fi
                else
                    if [[ ${apk_path} == *base.apk ]]; then
                        if ! test -f "${dl}/${base}"; then
                            print ${g} "Pulling ${y}${base} ($(${as} du -sh ${apk_path} | sed 's/\s.*//g'))${g}..."
                            adb -s ${device_id} pull ${apk_path} "${dl}/${base}" >/dev/null && : $((base_pulled++))
                        fi
                    else
                        mkdir -p "${dl}/${pkg}_${vcode}"
                        split_name="${dl}/${pkg}_${vcode}/$(sed 's/.*split_//g' <<<${apk_path})"
                        if ! test -f ${split_name}; then
                            print ${g} "Pulling ${y}$(sed 's/.*split_//g' <<<${apk_path}) ($(${as} du -sh ${apk_path} | sed 's/\s.*//g'))${g}..."
                            adb -s ${device_id} pull ${apk_path} ${split_name} >/dev/null && : $((splits_pulled++))
                        fi
                    fi
                fi
            done
        else
            print ${r} "Unable to get paths for the apk files :(" && continue
        fi
        obb_format="main.${vcode}.${pkg}.obb"
        obb_path="/sdcard/Android/obb/${pkg}"
        if ${as} test -f "${obb_path}/${obb_format}"; then
            if ! test -f "${dl}/${obb_format}"; then
                print ${g} "Pulling ${y}${obb_format} ($(${as} du -sh "${obb_path}/${obb_format}" | sed 's/\s.*//g'))${g}..."
                adb -s ${device_id} pull "${obb_path}/${obb_format}" "${dl}/${obb_format}" >/dev/null && : $((obbs_pulled++))
            fi
        fi
        if ${as} test -f "${obb_path}/${obb_format/main/patch}"; then
            if ! test -f "${dl}/${obb_format/main/patch}"; then
                print ${g} "Pulling ${y}${obb_format/main/patch} ($(${as} du -sh "${obb_path}/${obb_format/main/patch}" | sed 's/\s.*//g'))${g}..."
                adb -s ${device_id} pull "${obb_path}/${obb_format/main/patch}" "${dl}/${obb_format/main/patch}" >/dev/null && : $((obbs_pulled++))
            fi
        fi
        if [[ ${@} =~ --uninstall ]]; then
            print ${g} "Uninstalling ${y}${pkg}${g}..."
            ${as} pm uninstall ${pkg} &>/dev/null
        fi

        restore_stay_on
        : $((successful_actions++))
        pulled=$((base_pulled+splits_pulled+obbs_pulled))
        if [[ ${pulled} -gt 0 ]]; then
            print ${g} "The operation was completed successfully! ${pulled} files pulled ${y}(${base_pulled} base, ${splits_pulled} splits and ${obbs_pulled} obb's)${g} into ${y}${dl}"
        else
            print ${y} "The files already exist, nothing has been downloaded."
        fi
    fi
done

[[ ${successful_actions} == ${actions} ]] && color=${g} || color=${r}; unset device_model
print ${color} "${successful_actions}/${actions} successful operations!" && exit $((actions - successful_actions))
