#!/usr/bin/env bash
# Creates two Mac apps for ClipSafe AI and puts "ClipSafe AI" in the Dock:
#   ClipSafe AI.app    click to start (in the background) and open the dashboard
#   Stop ClipSafe.app  click to stop it
# The apps are built on this Mac with osacompile, so macOS does not quarantine them.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CTL="${ROOT}/local/clipsafe-ctl.sh"
chmod +x "${ROOT}/local/"*.sh "${ROOT}/local/start-mac.command" 2>/dev/null || true
xattr -dr com.apple.quarantine "${ROOT}/local" 2>/dev/null || true

DEST="/Applications"
if [[ ! -w "${DEST}" ]]; then
  DEST="${HOME}/Applications"
  mkdir -p "${DEST}"
fi
START_APP="${DEST}/ClipSafe AI.app"
STOP_APP="${DEST}/Stop ClipSafe.app"

# AppleScript string literal for the control script path
CTL_AS="$(printf '%s' "${CTL}" | sed 's/\\/\\\\/g; s/"/\\"/g')"

TMP="$(mktemp -d)"
trap 'rm -rf "${TMP}"' EXIT

cat >"${TMP}/start.applescript" <<ASEOF
set ctl to "${CTL_AS}"
try
	set state to do shell script "/bin/bash " & quoted form of ctl & " status"
	if state is not "running" then
		display notification "Starting… the dashboard opens in about 20 seconds." with title "ClipSafe AI"
		do shell script "/bin/bash " & quoted form of ctl & " start"
	end if
	open location "http://localhost:3000"
on error errMsg
	display dialog "ClipSafe AI could not start." & return & return & errMsg buttons {"OK"} default button "OK" with icon stop with title "ClipSafe AI"
end try
ASEOF

cat >"${TMP}/stop.applescript" <<ASEOF
set ctl to "${CTL_AS}"
do shell script "/bin/bash " & quoted form of ctl & " stop"
display notification "ClipSafe AI has stopped." with title "ClipSafe AI"
ASEOF

rm -rf "${START_APP}" "${STOP_APP}"
osacompile -o "${START_APP}" "${TMP}/start.applescript"
osacompile -o "${STOP_APP}" "${TMP}/stop.applescript"

# Icon from the dashboard's SVG logo (best effort; the default icon is used if this fails)
SVG="${ROOT}/frontend/app/icon.svg"
if [[ -f "${SVG}" ]] && qlmanage -t -s 1024 -o "${TMP}" "${SVG}" >/dev/null 2>&1 && [[ -f "${TMP}/icon.svg.png" ]]; then
  ICONSET="${TMP}/ClipSafe.iconset"
  mkdir -p "${ICONSET}"
  for s in 16 32 128 256 512; do
    sips -z "$s" "$s" "${TMP}/icon.svg.png" --out "${ICONSET}/icon_${s}x${s}.png" >/dev/null 2>&1 || true
    d=$((s * 2))
    sips -z "$d" "$d" "${TMP}/icon.svg.png" --out "${ICONSET}/icon_${s}x${s}@2x.png" >/dev/null 2>&1 || true
  done
  if iconutil -c icns "${ICONSET}" -o "${TMP}/ClipSafe.icns" 2>/dev/null; then
    for app in "${START_APP}" "${STOP_APP}"; do
      cp "${TMP}/ClipSafe.icns" "${app}/Contents/Resources/applet.icns"
      touch "${app}"
    done
  fi
fi

# Re-sign after changing the icon so Apple Silicon Macs still open the apps
for app in "${START_APP}" "${STOP_APP}"; do
  codesign --force --deep --sign - "${app}" >/dev/null 2>&1 || true
  xattr -dr com.apple.quarantine "${app}" 2>/dev/null || true
done

# Add "ClipSafe AI" to the Dock once
if ! defaults read com.apple.dock persistent-apps 2>/dev/null | grep -q "ClipSafe%20AI.app"; then
  defaults write com.apple.dock persistent-apps -array-add \
    "<dict><key>tile-data</key><dict><key>file-data</key><dict><key>_CFURLString</key><string>file://${START_APP// /%20}/</string><key>_CFURLStringType</key><integer>15</integer></dict></dict></dict>"
  killall Dock 2>/dev/null || true
fi

echo "✓ Created \"${START_APP}\" (added to the Dock) and \"${STOP_APP}\"."
echo "Click the ClipSafe AI icon in the Dock to start. Use Stop ClipSafe (in ${DEST}) to stop it."
