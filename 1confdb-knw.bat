@echo off
rem Запуск MCP-сервера 1confdb-knw (знания по конфигурации 1С и BSL) через venv проекта
"%~dp0.venv\Scripts\python.exe" -m confdb.mcp_server %*
