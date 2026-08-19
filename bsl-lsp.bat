@echo off
rem BSL Language Server (форк bsl-language-server-confdb): анализ, LSP, WebSocket, MCP.
rem Примеры:
rem   bsl-lsp.bat analyze -s <workspace> -r json -o <каталог-отчёта>
rem   bsl-lsp.bat lsp            (stdio-режим для редактора)
rem   bsl-lsp.bat mcp            (MCP-режим)
setlocal
set "JAR=%~dp0bin\bsl-language-server.jar"
if not exist "%JAR%" (
    echo Не найден jar: %JAR%
    echo Соберите форк bsl-language-server-confdb: запустите build-lsp-jar.bat
    exit /b 2
)
if defined JAVA_HOME (
    set "JAVA=%JAVA_HOME%\bin\java.exe"
) else (
    set "JAVA=java"
)
"%JAVA%" -jar "%JAR%" %*
