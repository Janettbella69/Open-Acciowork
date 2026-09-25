#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h:h}
python_executable=${OPHIRA_DESKTOP_PYTHON:-${repo_dir}/.venv/bin/python}
app_dir=${script_dir}/build/OPHIRA.app
contents=${app_dir}/Contents

if [[ ! -x ${python_executable} ]]; then
  print -u2 "Python runtime is missing: ${python_executable}"
  exit 1
fi
if [[ ! -f ${repo_dir}/frontend/dist/index.html ]]; then
  print -u2 "Build the frontend first: cd frontend && npm run build"
  exit 1
fi

mkdir -p "${contents}/MacOS" "${contents}/Resources/backend"
swiftc -framework AppKit -framework WebKit -framework Security \
  "${script_dir}/OphiraDesktop.swift" -o "${contents}/MacOS/OPHIRA"
rsync -a --delete "${repo_dir}/frontend/dist/" "${contents}/Resources/ui/"
rsync -a --delete --delete-excluded --exclude '__pycache__/' --exclude '*.pyc' \
  "${repo_dir}/backend/src/" "${contents}/Resources/backend/src/"
rsync -a --delete --delete-excluded --exclude '__pycache__/' --exclude '*.pyc' \
  "${repo_dir}/backend/ophira-capabilities/" \
  "${contents}/Resources/backend/ophira-capabilities/"

"${python_executable}" - "${contents}/Info.plist" "${python_executable}" <<'PY'
import plistlib
import sys

with open(sys.argv[1], "wb") as output:
    plistlib.dump({
        "CFBundleExecutable": "OPHIRA",
        "CFBundleIdentifier": "com.ophira.desktop.dev",
        "CFBundleName": "OPHIRA",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSScreenCaptureUsageDescription": (
            "Capture a selected app window as evidence after approval."
        ),
        "NSMicrophoneUsageDescription": "Transcribe voice input when you request it.",
        "NSSpeechRecognitionUsageDescription": (
            "Transcribe voice input when you request it."
        ),
        "OphiraPythonExecutable": sys.argv[2],
    }, output)
PY

print "Built ${app_dir} (development runtime: ${python_executable})"
