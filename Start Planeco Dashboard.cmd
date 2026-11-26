@echo off
setlocal DisableDelayedExpansion
pushd "%~dp0" || exit /b 1

if exist ".venv\Scripts\python.exe" goto use_venv
where.exe /q py.exe
if not errorlevel 1 goto use_py
where.exe /q uv.exe
if not errorlevel 1 goto use_uv
where.exe /q python.exe
if not errorlevel 1 goto use_python
echo Install Python 3.11 or newer from https://www.python.org/downloads/
goto failed

:use_venv
".venv\Scripts\python.exe" "launch_dashboard.py" %*
goto finished

:use_py
py -3 -c "import sys; raise SystemExit(sys.version_info[:2] < (3, 11))" >nul 2>&1
if not errorlevel 1 (
    py -3 "launch_dashboard.py" %*
    goto finished
)
where.exe /q uv.exe
if not errorlevel 1 goto use_uv

:use_python
python "launch_dashboard.py" %*
goto finished

:use_uv
uv run --no-project --no-cache --no-python-downloads --python ">=3.11" -- python "launch_dashboard.py" %*

:finished
if errorlevel 1 goto failed
popd
exit /b 0

:failed
echo.
echo Planeco could not start. Check the message above.
popd
pause
exit /b 1
