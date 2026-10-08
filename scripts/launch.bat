@echo off
setlocal
cd /d "%~dp0.."
if errorlevel 1 goto :failed
set "APP_ARGS="
set "CONSOLE_MODE=0"
set "FORCE_INSTALL=0"

:parse_args
if "%~1"=="" goto :check_environment
if /i "%~1"=="--debug" goto :arg_debug
if /i "%~1"=="--safe-layout" goto :arg_safe
if /i "%~1"=="--perf-diag" goto :arg_perf
if /i "%~1"=="--install" goto :arg_install
echo [ERROR] Supported options: --debug --safe-layout --perf-diag --install
pause
exit /b 2

:arg_debug
set "CONSOLE_MODE=1"
shift
goto :parse_args
:arg_safe
set "APP_ARGS=%APP_ARGS% --safe-layout"
shift
goto :parse_args
:arg_perf
set "APP_ARGS=%APP_ARGS% --perf-diag"
set "CONSOLE_MODE=1"
shift
goto :parse_args
:arg_install
set "FORCE_INSTALL=1"
shift
goto :parse_args

:check_environment
if not exist "runtime\.venv\Scripts\python.exe" goto :create_environment
if not exist "runtime\.venv\Scripts\pythonw.exe" goto :broken_environment
if "%FORCE_INSTALL%"=="1" goto :install_dependencies
"runtime\.venv\Scripts\python.exe" -c "import PySide6.QtWidgets" >nul 2>&1
if errorlevel 1 goto :install_dependencies
goto :launch

:create_environment
echo Manga Rename Tool - First Setup
where py >nul 2>&1
if not errorlevel 1 goto :use_py
where python >nul 2>&1
if not errorlevel 1 goto :use_python
echo [ERROR] Python was not found. Install Python 3.11 or newer.
echo Enable Add Python to PATH, then run run.bat again.
pause
exit /b 1
:use_py
set "TOOL_PYTHON=py"
set "TOOL_PYARGS=-3"
goto :create_venv
:use_python
set "TOOL_PYTHON=python"
set "TOOL_PYARGS="
:create_venv
if not exist "runtime" mkdir "runtime"
if errorlevel 1 goto :failed
%TOOL_PYTHON% %TOOL_PYARGS% -m venv "runtime\.venv"
if errorlevel 1 goto :failed

:install_dependencies
echo Installing or checking dependencies. Internet access is required.
"runtime\.venv\Scripts\python.exe" -m pip install -r "scripts\requirements.txt"
if errorlevel 1 goto :failed
"runtime\.venv\Scripts\python.exe" -c "import PySide6.QtWidgets"
if errorlevel 1 goto :failed

:launch
if "%CONSOLE_MODE%"=="1" goto :console_launch
start "" "runtime\.venv\Scripts\pythonw.exe" -m src.main %APP_ARGS%
if errorlevel 1 goto :failed
exit /b 0

:console_launch
"runtime\.venv\Scripts\python.exe" -m src.main %APP_ARGS%
set "APP_RC=%ERRORLEVEL%"
echo.
echo Application exit code: %APP_RC%
echo Logs: logs
pause
exit /b %APP_RC%

:broken_environment
echo [ERROR] The local Python environment is incomplete.
echo Delete only the runtime folder, then run run.bat again.
echo Keep the data folder.
pause
exit /b 1

:failed
echo.
echo [ERROR] Setup or launch failed. Send a screenshot of this window.
echo For startup errors, check logs or use scripts\debug_run.bat.
pause
exit /b 1
