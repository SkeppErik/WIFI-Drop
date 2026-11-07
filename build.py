"""Builds the WiFiDrop desktop program for the current OS.

    Linux:   python3 build.py   ->  dist/WiFiDrop        (single executable)
    Windows: py build.py        ->  dist\\WiFiDrop.exe    (single executable)

PyInstaller can't cross-compile: build the .exe on Windows (or via build-windows-wine.sh).
"""
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))
os.chdir(here)

subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", "pyinstaller"])

subprocess.check_call([sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "wifidrop.spec"])
print("\nBuilt:", os.path.join(here, "dist", "WiFiDrop.exe" if os.name == "nt" else "WiFiDrop"))
