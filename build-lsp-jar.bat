@echo off
rem Builds the bsl-language-server-confdb fork (gradlew bootJar), then copies
rem the executable jar to bin\bsl-language-server.jar.
rem Usage: build-lsp-jar.bat [path-to-fork]
rem By default the fork is searched for next to the main project repository.
rem Pure ASCII: readable in any console code page (866/1251/65001).
setlocal
set "FORK=%~1"
if "%FORK%"=="" set "FORK=%~dp0..\..\..\bsl-language-server-confdb"
if not exist "%FORK%\gradlew.bat" (
    echo Fork not found: %FORK%
    echo Pass the path as the first argument: build-lsp-jar.bat D:\path\to\bsl-language-server-confdb
    exit /b 2
)
if not defined JAVA_HOME (
    echo JAVA_HOME is not set - JDK 21 required
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
    echo Copied: %%j -^> bin\bsl-language-server.jar
    goto :done
)
echo jar not found in %FORK%\build\libs
exit /b 2
:done
