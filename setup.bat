@echo off
rem Настройка окружения confdb: создание .venv и установка пакета.
rem Запускать двойным кликом или из консоли: setup.bat
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    echo venv уже существует: .venv
    goto install
)

echo Создаю виртуальное окружение в .venv ...
python -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" py -3 -m venv .venv 2>nul
if not exist ".venv\Scripts\python.exe" (
    echo Ошибка: Python 3.9+ не найден в PATH. Установите Python и повторите.
    exit /b 1
)

:install
echo Устанавливаю confdb в venv ...
".venv\Scripts\python.exe" -m pip install --upgrade pip
".venv\Scripts\python.exe" -m pip install .
if errorlevel 1 (
    echo Повторяю установку без изоляции сборки ...
    ".venv\Scripts\python.exe" -m pip install --no-build-isolation .
)
if errorlevel 1 (
    echo Ошибка установки пакета.
    exit /b 1
)

echo.
echo Готово. Запуск:
echo   confdb.bat extract файл.cf --db out.sqlite [--dump каталог]
echo   confdb-ui.bat              текстовый консольный интерфейс
endlocal

:jdk
where java >nul 2>nul
if not errorlevel 1 goto jdk_ok
if defined JAVA_HOME if exist "%JAVA_HOME%\bin\java.exe" goto jdk_ok
where winget >nul 2>nul
if errorlevel 1 (
    echo Внимание: java не найдена, winget недоступен.
    echo Для инструментов bsl_* установите JDK 21 вручную и выполните build-lsp-jar.bat
    goto :eof
)
set /p INSTALL_JDK=Java не найдена. Установить JDK 21 Temurin через winget - может потребоваться подтверждение администратора? [Y/n]: 
if /i "%INSTALL_JDK%"=="n" goto :eof
if /i "%INSTALL_JDK%"=="Н" goto :eof
if /i "%INSTALL_JDK%"=="нет" goto :eof
echo Устанавливаю JDK 21 Temurin ...
winget install --id EclipseAdoptium.Temurin.21.JDK -e --accept-source-agreements --accept-package-agreements
if errorlevel 1 (
    echo Ошибка установки JDK. Установите вручную: winget install EclipseAdoptium.Temurin.21.JDK
    goto :eof
)
for /d %%d in ("C:\Program Files\Eclipse Adoptium\jdk-21*") do set "JAVA_HOME=%%d"
if defined JAVA_HOME echo JAVA_HOME для этой консоли: %JAVA_HOME% - можно сразу запускать build-lsp-jar.bat

:jdk_ok
echo.
echo Для инструментов bsl_* - BSL Language Server в MCP-режиме:
echo   build-lsp-jar.bat          соберёт jar в bin\
echo   1confdb-knw.bat out.db --lsp-workspace каталог-дампа
