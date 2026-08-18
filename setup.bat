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
