@echo off
setlocal
set "PYTHONPATH=%~dp0src"
"%~dp0..\..\.venv\Scripts\python.exe" -m pytest "%~dp0tests" %*
