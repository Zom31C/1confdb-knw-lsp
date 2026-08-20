@echo off
rem 1confdb-knw MCP server launcher (knowledge base for 1C configuration + BSL),
rem via project venv. Pure ASCII wrapper: user-facing text is printed by the
rem Python layer (UTF-8), so this file is immune to the console code page.
"%~dp0.venv\Scripts\python.exe" -m confdb.mcp_server %*
