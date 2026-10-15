@echo off
rem Entry point for cmd.exe: `kit <command>`, the same commands as `./kit` on Linux.
rem
rem The real entry point is `kit`, a Python script with no extension: a shebang means nothing here and
rem cmd.exe will not run it, so this finds an interpreter and hands the arguments straight over. `py` (the
rem PEP 397 launcher, installed with Python itself) is tried first because it is the only name that is
rem reliably Python 3; `python3` is tried last because on a machine without Python that name is the Store
rem alias, a stub that opens the Microsoft Store. KIT_PYTHON overrides all of it.
setlocal
set "KIT=%~dp0kit"
if defined KIT_PYTHON goto :custom
py -3 -c "" >nul 2>&1
if not errorlevel 1 goto :launcher
python -c "" >nul 2>&1
if not errorlevel 1 goto :python
python3 -c "" >nul 2>&1
if not errorlevel 1 goto :python3
echo kit: no Python 3 found on PATH. Install Python 3.11 or newer 1>&2
echo      ^(python.org, or: winget install Python.Python.3.12^), 1>&2
echo      or set KIT_PYTHON to the interpreter to use. 1>&2
exit /b 2

:custom
"%KIT_PYTHON%" "%KIT%" %*
exit /b %ERRORLEVEL%

:launcher
py -3 "%KIT%" %*
exit /b %ERRORLEVEL%

:python
python "%KIT%" %*
exit /b %ERRORLEVEL%

:python3
python3 "%KIT%" %*
exit /b %ERRORLEVEL%
