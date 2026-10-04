@echo off
setlocal EnableExtensions
cd /d "%~dp0"
title PO Summary App

echo ==================================================
echo   PO Summary App
echo ==================================================
echo.

rem ---------- 1) Find a real Python 3.10+ ----------
set "PY="
call :trypy py -3
if not defined PY call :trypy python
if not defined PY call :trypy python3
if not defined PY goto :nopython
echo [OK] Python found: %PY%

rem ---------- 2) Check the virtual environment ----------
rem A .venv copied from another PC does not work - rebuild it if broken.
set "VPY=%~dp0.venv\Scripts\python.exe"
if not exist "%VPY%" goto :makevenv
"%VPY%" -c "import streamlit, pymupdf, fontTools, openpyxl, pandas" >nul 2>&1
if not errorlevel 1 goto :run
echo [!] Existing .venv is broken or incomplete - rebuilding it...
rmdir /s /q ".venv"

:makevenv
echo [..] Creating virtual environment - first run only, please wait...
%PY% -m venv .venv
if errorlevel 1 goto :fail
"%VPY%" -m pip install --upgrade pip
"%VPY%" -m pip install -r requirements.txt
if errorlevel 1 goto :fail
echo [OK] Packages installed.

:run
echo.
echo [..] Starting app at http://localhost:8501
echo      Keep this window open while using the app. Close it to stop.
echo.
rem open the browser a few seconds after the server starts
start "" /min cmd /c "timeout /t 5 /nobreak >nul & start http://localhost:8501"
"%VPY%" -m streamlit run app.py --server.headless true --browser.gatherUsageStats false
if errorlevel 1 goto :fail
goto :end

:trypy
rem %* = candidate command. Accept only if it is a real Python 3.10+
%* -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if not errorlevel 1 set "PY=%*"
exit /b 0

:nopython
echo [X] Python 3.10 or newer was not found on this computer.
echo.
echo     1. Download Python from https://www.python.org/downloads/
echo     2. In the installer, TICK "Add python.exe to PATH"
echo     3. Run run.bat again
echo.
pause
exit /b 1

:fail
echo.
echo [X] Something went wrong - see the messages above.
echo     Common causes: no internet during first install,
echo     or the folder is read-only / inside a zip file - extract it first.
echo.
pause
exit /b 1

:end
pause
endlocal
