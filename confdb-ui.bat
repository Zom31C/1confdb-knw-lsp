@echo off
rem confdb text console UI launcher (project venv). Pure ASCII wrapper: the UI
rem itself is rendered by the Python layer (UTF-8), immune to the console code page.
"%~dp0.venv\Scripts\python.exe" -m confdb.tui %*
