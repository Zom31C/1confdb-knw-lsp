@echo off
rem Запуск текстового консольного интерфейса confdb через venv проекта
"%~dp0.venv\Scripts\python.exe" -m confdb.tui %*
