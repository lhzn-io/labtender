@echo off
rem Opens the compact labtender view in a small Edge app window.
rem
rem With LABTENDER_URL set (a labtender service on another machine), it only opens the window.
rem Otherwise it starts the labtender service in WSL first if it is not already answering on
rem localhost, provided WSL already has a labtender database. Closing the window leaves the
rem service running.
rem
rem   LABTENDER_URL     a remote labtender service, e.g. http://lab-server:8795 (never started here)
rem   LABTENDER_DISTRO  WSL distribution that runs a local service (default: the WSL default)
rem   LABTENDER_SERVE   command that starts a local service inside WSL (default: labtender serve)
rem   LABTENDER_PORT    local service port (default: 8795)
setlocal
rem A shell opened before `setx LABTENDER_URL` lacks the variable; read the saved user value.
if not defined LABTENDER_URL (
    for /f "tokens=2,*" %%a in ('reg query HKCU\Environment /v LABTENDER_URL 2^>nul ^| %SystemRoot%\System32\find.exe "LABTENDER_URL"') do set "LABTENDER_URL=%%b"
)
if defined LABTENDER_URL (
    set URL=%LABTENDER_URL%
    goto :open
)
if not defined LABTENDER_PORT set LABTENDER_PORT=8795
if not defined LABTENDER_SERVE set LABTENDER_SERVE=labtender serve --port %LABTENDER_PORT%
set URL=http://localhost:%LABTENDER_PORT%
set DISTRO_ARGS=
if defined LABTENDER_DISTRO set DISTRO_ARGS=-d %LABTENDER_DISTRO%

curl.exe -s -o nul --max-time 2 %URL%/healthz
if errorlevel 1 (
    rem Starting without a database would create an empty one and show an empty board.
    wsl.exe %DISTRO_ARGS% -e bash -lc "test -e ${LABTENDER_HOME:-$HOME/.config/labtender}/labtender.sqlite"
    if errorlevel 1 (
        echo No labtender database in WSL and LABTENDER_URL is not set.
        echo Set LABTENDER_URL to your labtender service, for example:
        echo     setx LABTENDER_URL http://lab-server:8795
        echo or run "labtender serve" once in WSL to start a service here.
        pause
        exit /b 1
    )
    echo starting the labtender service in WSL
    start "labtender service" /min wsl.exe %DISTRO_ARGS% -e bash -lc "exec %LABTENDER_SERVE%"
    for /l %%i in (1,1,20) do (
        rem ping, not timeout: timeout fails when stdin is redirected (scheduled or scripted starts)
        ping -n 2 127.0.0.1 > nul
        curl.exe -s -o nul --max-time 2 %URL%/healthz && goto :open
    )
    echo the labtender service did not answer on %URL%; see the "labtender service" window
    exit /b 1
)

:open
set EDGE=%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe
if not exist "%EDGE%" set EDGE=%ProgramFiles%\Microsoft\Edge\Application\msedge.exe
if exist "%EDGE%" (
    start "" "%EDGE%" --app=%URL%/compact --window-size=360,640
) else (
    start "" %URL%/compact
)
