@echo off
setlocal
call "%~dp0scripts\launch.bat" %*
exit /b %ERRORLEVEL%
