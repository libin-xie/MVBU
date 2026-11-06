@echo off
rem Double-click to register the MVBU helper with Chrome, Chromium, Edge and Brave.
rem It runs install_chrome_host.py with Python. No PowerShell script is used, so
rem PowerShell execution policies such as "signed scripts only" do not apply.
rem Options: --python C:\Path\python.exe   --extension-id ID   --uninstall
setlocal
set "PYCMD="
py -3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYCMD=py -3"
if not defined PYCMD python -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYCMD=python"
if not defined PYCMD python3 -c "import sys; sys.exit(sys.version_info < (3, 8))" >nul 2>nul && set "PYCMD=python3"
if not defined PYCMD goto nopython
%PYCMD% "%~dp0install_chrome_host.py" %*
goto end
:nopython
echo Python 3.8 or later was not found.
echo Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH",
echo then run: python -m pip install uopy
echo and run this file again.
:end
echo.
pause
