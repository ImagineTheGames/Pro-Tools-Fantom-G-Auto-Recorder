@echo off
REM Wrapper so you can type `.\fs <command>` instead of the full Python path.
REM Uses PYTHON if set, otherwise whatever `python` resolves to on PATH.
setlocal
if defined PYTHON (
  "%PYTHON%" "%~dp0fantom_stem.py" %*
) else (
  python "%~dp0fantom_stem.py" %*
)
