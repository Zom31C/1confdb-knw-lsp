@echo off
rem Запуск CLI confdb через venv проекта
"%~dp0.venv\Scripts\python.exe" -m confdb %*
