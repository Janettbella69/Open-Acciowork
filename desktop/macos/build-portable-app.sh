#!/bin/zsh
set -euo pipefail

script_dir=${0:A:h}
repo_dir=${script_dir:h:h}
python_executable=${OPHIRA_DESKTOP_PYTHON:-${repo_dir}/.venv/bin/python}
output_dir=${script_dir}/build
final_app=${output_dir}/OPHIRA-portable.app

if [[ ! -x ${python_executable} ]]; then
  print -u2 "Python build environment is missing: ${python_executable}"
  exit 1
fi
if ! "${python_executable}" -m PyInstaller --version >/dev/null 2>&1; then
  print -u2 "Install backend[desktop-build] into the Python build environment first"
  exit 1
fi

mkdir -p "${output_dir}"
build_tmp=$(mktemp -d "${output_dir}/.portable.XXXXXX")
trap 'rm -rf -- "${build_tmp}"' EXIT
app_dir=${build_tmp}/OPHIRA.app

OPHIRA_DESKTOP_APP_DIR=${app_dir} OPHIRA_DESKTOP_PYTHON=${python_executable} \
  zsh "${script_dir}/build-app.sh"

"${python_executable}" -m PyInstaller \
  --noconfirm --onedir --name OPHIRA-sidecar \
  --paths "${repo_dir}/backend/src" \
  --hidden-import ophira_backend.main \
  --collect-all claude_agent_sdk \
  --distpath "${build_tmp}/dist" \
  --workpath "${build_tmp}/work" \
  --specpath "${build_tmp}/spec" \
  "${script_dir}/sidecar_entry.py"

contents=${app_dir}/Contents
frozen=${build_tmp}/dist/OPHIRA-sidecar
if [[ ! -x ${frozen}/OPHIRA-sidecar \
      || ! -x ${frozen}/_internal/claude_agent_sdk/_bundled/claude ]]; then
  print -u2 "The frozen sidecar or bundled Claude CLI is incomplete"
  exit 1
fi
mkdir -p "${contents}/Resources/sidecar"
rsync -a --delete "${frozen}/" "${contents}/Resources/sidecar/"
# The frozen executable contains the backend modules; the Plugin remains a
# separate read-only package under Resources/backend/ophira-capabilities.
rm -rf -- "${contents}/Resources/backend/src"

"${python_executable}" - "${contents}/Info.plist" <<'PY'
import plistlib
import sys

path = sys.argv[1]
with open(path, "rb") as source:
    info = plistlib.load(source)
info.pop("OphiraPythonExecutable", None)
with open(path, "wb") as output:
    plistlib.dump(info, output)
PY

# Ad-hoc signing makes local arm64 binaries loadable. Distribution needs a
# developer identity, hardened runtime, notarization, and clean-Mac validation.
codesign --force --deep --sign - "${app_dir}"
codesign --verify --deep --strict "${app_dir}"

rm -rf -- "${final_app}"
mv "${app_dir}" "${final_app}"
print "Built ${final_app} (bundled Python sidecar; ad-hoc signed for local testing)"
