@echo off
rem ════════════════════════════════════════════════════════════════
rem  Any WALLEX Portable — zero-install launcher
rem  Uses the bundled Python runtime (runtime\python). No install needed.
rem ════════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"

set PY=%~dp0runtime\python\python.exe

if not exist "%PY%" (
  echo [!] Bundled runtime missing: runtime\python\python.exe
  echo     Re-download the official release zip from this repository.
  pause
  exit /b 1
)

echo ==================================================
echo    Any WALLEX Portable
echo ==================================================
echo    Bundled runtime: Python 3.11 
echo    Mode: PAPER TRADING (no real orders)
echo.

rem ── first boot: deps are pre-installed in the bundled runtime ──
rem    (pip install is skipped; requirements are baked into runtime\python)

rem ── start the launcher (profile picker + dashboard auto-open) ──
"%PY%" launcher.py %*
if errorlevel 1 pause
