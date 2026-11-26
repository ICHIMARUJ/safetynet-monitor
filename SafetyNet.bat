@echo off
REM Safety Net 실행 (창 없이 백그라운드로 뜨고, 트레이 아이콘이 생김)
cd /d "%~dp0"
set "PYW="
if exist "data\pythonw.txt" set /p PYW=<"data\pythonw.txt"
if defined PYW if not exist "%PYW%" set "PYW="
if not defined PYW if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe" set "PYW=%LOCALAPPDATA%\Python\pythoncore-3.14-64\pythonw.exe"
if not defined PYW for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\pythonw.exe" set "PYW=%%D\pythonw.exe"
if not defined PYW set "PYW=pyw"
start "" "%PYW%" app.py %*
