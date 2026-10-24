@echo off
REM ============================================================
REM  Review Agent console launcher (ASCII only, CRLF)
REM  Usage: start-console.bat [port] [--no-browser] [--check]
REM  NOTE: keep this file ASCII-only. Non-ASCII bytes in a .bat
REM        can desync cmd parsing under the OEM code page.
REM ============================================================
setlocal EnableExtensions
cd /d "%~dp0"

set "PORT=8000"
set "NOBROWSER="
set "CHECKONLY="

:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--help" goto :usage
if /i "%~1"=="-h" goto :usage
if /i "%~1"=="--no-browser" goto :arg_nobrowser
if /i "%~1"=="--check" goto :arg_check
echo %~1| findstr /r "^[0-9][0-9]*$" >nul
if not errorlevel 1 set "PORT=%~1"
shift
goto :parse

:arg_nobrowser
set "NOBROWSER=1"
shift
goto :parse

:arg_check
set "CHECKONLY=1"
shift
goto :parse

:parsed
echo %PORT%| findstr /r "^[0-9][0-9]*$" >nul
if errorlevel 1 set "PORT=8000"

if not exist "%~dp0logs" mkdir "%~dp0logs"
set "LOG=%~dp0logs\console-boot.log"
call :log "boot: port=%PORT%"

REM ---------- locate a working Python 3 ----------
set "PY="
set "PYARGS="
if defined REVIEW_AGENT_PY if exist "%REVIEW_AGENT_PY%" set "PY=%REVIEW_AGENT_PY%"
if not defined PY if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0.venv\bin\python" set "PY=%~dp0.venv\bin\python"

if not defined PY (
  for /f "delims=" %%i in ('where python 2^>nul') do (
    if not defined PY (
      "%%i" -c "import sys; raise SystemExit(0 if sys.version_info[0]==3 else 1)" >nul 2>nul
      if not errorlevel 1 set "PY=%%i"
    )
  )
)
if not defined PY (
  where py >nul 2>nul
  if not errorlevel 1 (set "PY=py" & set "PYARGS=-3")
)

if not defined PY (
  echo [ERROR] Python 3 not found.
  echo         Install Python 3, or set REVIEW_AGENT_PY to python.exe.
  call :log "ERROR: python not found"
  pause
  exit /b 1
)

REM ---------- check mode ----------
if defined CHECKONLY (
  echo   Python : %PY% %PYARGS%
  "%PY%" %PYARGS% -c "import sys; print('  version:', sys.version.split()[0])"
  "%PY%" %PYARGS% -c "import yaml; print('  pyyaml : OK')"
  if errorlevel 1 goto :fail_yaml
  "%PY%" %PYARGS% "%~dp0tools\llm_client.py" --check
  call :log "check: OK"
  exit /b 0
)

REM ---------- dependency check ----------
"%PY%" %PYARGS% -c "import yaml" >nul 2>nul
if not errorlevel 1 goto :ready

echo [INFO] pyyaml not found. Creating project venv .venv ...
call :log "pyyaml missing; creating .venv"
"%PY%" %PYARGS% -m venv "%~dp0.venv"
if errorlevel 1 goto :fail_venv
set "PY=%~dp0.venv\Scripts\python.exe"
set "PYARGS="
"%PY%" -m pip install --quiet --upgrade pip
"%PY%" -m pip install --quiet pyyaml
if errorlevel 1 goto :fail_yaml

:ready
echo   Python: %PY%
call :log "python=%PY%"

REM ---------- port check ----------
netstat -an 2>nul | findstr /c:":%PORT% " | findstr "LISTENING" >nul
if not errorlevel 1 goto :reuse

echo [INFO] Starting review console on port %PORT% ...
call :log "starting server on %PORT%"
start "ReviewAgent Console" "%PY%" %PYARGS% "%~dp0server.py" --port %PORT%
goto :wait

:reuse
where curl >nul 2>nul
if errorlevel 1 goto :reuse_ok
curl -s -f -o nul --max-time 1 "http://127.0.0.1:%PORT%/api/targets"
if errorlevel 1 goto :port_busy
:reuse_ok
echo [INFO] Port %PORT% is already serving the console; reusing it.
call :log "reuse existing console on %PORT%"

:wait
ping -n 3 127.0.0.1 >nul
if defined NOBROWSER (
  echo [INFO] Console: http://127.0.0.1:%PORT%
  goto :done
)
echo [INFO] Opening http://127.0.0.1:%PORT%
start "" "http://127.0.0.1:%PORT%"

:done
call :log "done"
exit /b 0

:port_busy
echo [ERROR] Port %PORT% is in use by another program.
echo         Choose another port, e.g. start-console.bat 8080
call :log "ERROR: port %PORT% busy"
pause
exit /b 1

:fail_venv
echo [ERROR] Failed to create .venv.
echo         Run manually: "%PY%" -m venv .venv
call :log "ERROR: venv failed"
if defined CHECKONLY exit /b 2
pause
exit /b 1

:fail_yaml
echo [ERROR] pyyaml is missing or failed to install.
echo         Run manually: "%PY%" -m pip install pyyaml
call :log "ERROR: pyyaml failed"
if defined CHECKONLY exit /b 2
pause
exit /b 1

:log
echo [%time%] %~1>>"%LOG%"
exit /b 0

:usage
echo Usage: start-console.bat [port] [--no-browser] [--check] [--help]
echo.
echo   port         HTTP port, default 8000
echo   --no-browser start server but do not open a browser
echo   --check      print Python / pyyaml / LLM config and exit
echo   --help       show this help
exit /b 0
