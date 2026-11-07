#!/usr/bin/env bash
# Builds dist/windows/WiFiDrop.exe on Linux using Wine (PyInstaller can't cross-compile).
# First run sets up Windows Python in ./.wine (~300 MB); later runs just rebuild (~1 min).
#
# Wine quirks handled here:
#  - pip inside Wine hangs on HTTPS and on building source packages, so the Windows
#    wheels are downloaded with Linux pip and installed offline.
#  - Wine Python crashes if stdout is redirected to a file directly, so output goes through cat.
set -euo pipefail
cd "$(dirname "$0")"
command -v wine >/dev/null || { echo "Wine is not installed (sudo apt install wine)"; exit 1; }

export WINEPREFIX="$PWD/.wine" WINEDEBUG=-all
PY='C:\Python312\python.exe'
PYVER=3.12.10
CACHE="$PWD/build/wine-cache"
mkdir -p "$CACHE/wheels"

# Linux-side helper environment (for pip download / wheel building)
[[ -x .venv/bin/pip ]] || python3 -m venv --system-site-packages .venv

if ! wine "$PY" --version >/dev/null 2>&1; then
  echo "== Installing Windows Python $PYVER into $WINEPREFIX"
  wineboot -i >/dev/null 2>&1 || true
  [[ -f "$CACHE/python.exe" ]] || curl -sSfL -o "$CACHE/python.exe" \
    "https://www.python.org/ftp/python/$PYVER/python-$PYVER-amd64.exe"
  WINEDLLOVERRIDES="mscoree,mshtml=" wine "$CACHE/python.exe" /quiet InstallAllUsers=0 PrependPath=0 \
    Include_test=0 Include_tcltk=0 Include_doc=0 Include_launcher=0 'TargetDir=C:\Python312' >/dev/null 2>&1
fi

if ! wine "$PY" -c "import webview, qrcode, PyInstaller" >/dev/null 2>&1; then
  echo "== Downloading Windows packages"
  W="$CACHE/wheels"
  PIPDL=(.venv/bin/pip download -q --disable-pip-version-check -d "$W")
  WIN=(--platform win_amd64 --python-version 3.12 --implementation cp --only-binary=:all:)
  "${PIPDL[@]}" --no-deps --only-binary=:all: "pywebview>=6"
  "${PIPDL[@]}" "${WIN[@]}" qrcode pyinstaller pythonnet bottle typing_extensions colorama pefile pywin32-ctypes setuptools wheel
  # proxy_tools only ships as source; build a universal wheel on Linux
  "${PIPDL[@]}" --no-deps proxy_tools
  .venv/bin/pip wheel -q --disable-pip-version-check --no-deps -w "$W" "$W"/proxy_tools-*.tar.gz
  rm -f "$W"/proxy_tools-*.tar.gz
  echo "== Installing them in Windows Python"
  wine "$PY" -m pip install -q --disable-pip-version-check --no-warn-script-location --no-index \
    --no-build-isolation --find-links "$(winepath -w "$W" 2>/dev/null)" pywebview qrcode pyinstaller 2>&1 | cat
fi

echo "== Building WiFiDrop.exe"
wine "$PY" -m PyInstaller --noconfirm --clean --log-level WARN \
  --distpath dist/windows --workpath build/windows wifidrop.spec 2>&1 | cat
ls -lh dist/windows/WiFiDrop.exe
