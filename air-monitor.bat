@echo off
rem Start Air Monitor on Windows: double-click this file, or run  air-monitor.bat [options]  (see --help).
rem Uses Python if it already has the packages; otherwise the first run sets up a private one in .venv.
setlocal
cd /d "%~dp0"
set "PY=python"
where py >nul 2>nul && set "PY=py -3"
%PY% -c "import numpy, scipy, pyqtgraph, PyQt6" >nul 2>nul
if errorlevel 1 (
    if not exist ".venv\Scripts\python.exe" (
        echo First run: installing what Air Monitor needs into .venv ^(this takes a minute^)...
        %PY% -m venv .venv
        if errorlevel 1 goto novenv
        ".venv\Scripts\python.exe" -m pip install --quiet --upgrade pip
        ".venv\Scripts\python.exe" -m pip install --quiet -r requirements.txt
        if errorlevel 1 goto nopackages
    )
    set "PY=.venv\Scripts\python.exe"
)
where hackrf_transfer >nul 2>nul || echo Note: the HackRF tools are not on the PATH ^(see the README^). The demo still works.
%PY% tools\air_monitor.py %*
if errorlevel 1 pause
exit /b

:novenv
echo Could not set up Python. Install Python 3 from python.org ^(tick "Add python.exe to PATH"^), then try again.
pause
exit /b 1

:nopackages
echo Installing the Python packages failed. Check the internet connection, then try again.
pause
exit /b 1
