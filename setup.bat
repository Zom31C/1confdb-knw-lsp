@echo off
rem confdb environment setup: create .venv and install the package.
rem Run by double-click or from a console: setup.bat
rem Pure ASCII: readable in any console code page (866/1251/65001); all other
rem user-facing text is printed by the Python layer in UTF-8.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo venv already exists: .venv
    goto install
)

echo Creating virtual environment in .venv ...
python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" (
    echo Error: Python 3.9+ not found in PATH. Install Python and re-run.
    exit /b 1
)

:install
echo Installing confdb into venv (editable) ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install -e .
if errorlevel 1 (
    echo Retrying install without build isolation ...
    ".venv\Scripts\python.exe" -m pip install --no-build-isolation -e .
)
if errorlevel 1 (
    echo Package installation failed.
    exit /b 1
)

echo.
echo Done. Usage:
echo   confdb.bat extract file.cf --db out.sqlite [--dump dir]
echo   confdb-ui.bat              text console interface

:jdk
where java >nul 2>nul
if not errorlevel 1 goto jdk_ok
if defined JAVA_HOME if exist "%JAVA_HOME%\bin\java.exe" goto jdk_ok
where winget >nul 2>nul
if errorlevel 1 (
    echo Warning: java not found and winget is unavailable.
    echo For bsl_* tools install JDK 21 manually, then run build-lsp-jar.bat
    goto :eof
)
set /p INSTALL_JDK=Java not found. Install JDK 21 Temurin via winget (may require admin approval)? [Y/n]: 
if /i "%INSTALL_JDK%"=="n" goto :eof
if /i "%INSTALL_JDK%"=="no" goto :eof
echo Installing JDK 21 Temurin ...
winget install --id EclipseAdoptium.Temurin.21.JDK -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo JDK install failed. Install manually: winget install EclipseAdoptium.Temurin.21.JDK
    goto :eof
)
for /d %%d in ("C:\Program Files\Eclipse Adoptium\jdk-21*") do set "JAVA_HOME=%%d"
if defined JAVA_HOME echo JAVA_HOME for this console: %JAVA_HOME% - you can run build-lsp-jar.bat right away

:jdk_ok
echo.
echo For bsl_* tools (BSL Language Server in MCP mode):
echo   build-lsp-jar.bat          builds the jar into bin\
echo   1confdb-knw.bat out.db --lsp-workspace dump-dir
