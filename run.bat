@echo off
rem Start the local QuotePrism web server and open it in the browser.
rem Pure ASCII on purpose: cmd parses batch files in ANSI.
setlocal
cd /d "%~dp0"
python web_server.py --port 8770 --open
if errorlevel 1 (
  echo.
  echo [ERROR] failed to start. Is Python 3.10+ installed and on PATH?
  pause
)
endlocal
