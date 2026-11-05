@echo off
rem code-redundancy-pruner Windows launcher. Python 3.9+ required, no pip deps.
setlocal
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
  where py >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
  echo [rcp] Python 3.9+ not found. Install Python 3, or set PYEXE before calling.
  exit /b 1
)
%PYEXE% "%~dp0scripts\rcp.py" %*
exit /b %ERRORLEVEL%
