@echo off
setlocal
title grp-mcp - launching config UI

rem Always use the connections.json sitting next to THIS script, no matter
rem what folder the double-click happened from. This is the fix for the
rem classic "config UI wrote to the wrong file" problem - it removes the
rem current-working-directory dependency entirely.
set "GRP_MCP_CONNECTIONS=%~dp0connections.json"

echo ============================================
echo   grp-mcp Configuration UI
echo ============================================
echo   Connections file: %GRP_MCP_CONNECTIONS%
echo ============================================
echo.

where uvx >nul 2>nul
if errorlevel 1 (
    echo ERROR: "uvx" was not found on your PATH.
    echo.
    echo Install it first, then close ALL terminal windows and re-run this:
    echo   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 ^| iex"
    echo.
    pause
    exit /b 1
)

echo Starting the config server in a new window...
start "grp-mcp config UI - close this window when you're done editing" cmd /k uvx --from grp-mcp grp-mcp-ui

echo Waiting for it to start...
timeout /t 3 /nobreak >nul

echo Opening your browser...
start "" http://127.0.0.1:8765

echo.
echo Done - edit your instance^(s^) in the browser tab that just opened.
echo When finished, close the "grp-mcp config UI" window to stop the server.
timeout /t 4 >nul
