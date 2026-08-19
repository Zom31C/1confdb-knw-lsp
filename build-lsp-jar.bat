@echo off
rem Собирает форк bsl-language-server-confdb: gradlew bootJar, затем копирует
rem исполняемый jar в bin\bsl-language-server.jar.
rem Использование: build-lsp-jar.bat [путь-к-форку]
rem По умолчанию форк ищется рядом с основным репозиторием проекта.
setlocal
set "FORK=%~1"
if "%FORK%"=="" set "FORK=%~dp0..\..\..\bsl-language-server-confdb"
if not exist "%FORK%\gradlew.bat" (
    echo Форк не найден: %FORK%
    echo Укажите путь первым аргументом: build-lsp-jar.bat D:\path\bsl-language-server-confdb
    exit /b 2
)
if not defined JAVA_HOME (
    echo Не задан JAVA_HOME - нужен JDK 21
    exit /b 2
)
pushd "%FORK%"
call gradlew.bat bootJar --console=plain -q
set "RC=%ERRORLEVEL%"
popd
if not "%RC%"=="0" exit /b %RC%
for /f "delims=" %%j in ('dir /b /o-d "%FORK%\build\libs\*-exec.jar"') do (
    if not exist "%~dp0bin" mkdir "%~dp0bin"
    copy /Y "%FORK%\build\libs\%%j" "%~dp0bin\bsl-language-server.jar"
    echo Скопировано: %%j -^> bin\bsl-language-server.jar
    goto :done
)
echo jar не найден в %FORK%\build\libs
exit /b 2
:done
