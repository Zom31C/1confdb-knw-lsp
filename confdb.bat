@echo off
rem confdb CLI launcher (project venv). All user-facing text is printed by the
rem Python layer (UTF-8), so this wrapper is kept pure ASCII to be immune to
rem the console code page (866/1251/65001).
"%~dp0.venv\Scripts\python.exe" -m confdb %*
