@echo off
REM Wrapper so you can type `.\fs <command>` instead of the full Python path.
"%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%~dp0fantom_stem.py" %*
