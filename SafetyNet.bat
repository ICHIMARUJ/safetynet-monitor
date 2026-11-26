@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYW="
if exist "data\pythonw.txt" set /p PYW=<"data\pythonw.txt"
if defined PYW if not exist "%PYW%" set "PYW="
if defined PYW goto run
call :findpy
if not defined PY goto nopy
set "PYW=%PY:python.exe=pythonw.exe%"
if not exist "%PYW%" set "PYW=%PY%"
:run
start "" "%PYW%" app.py %*
exit /b 0

:nopy
echo.
echo  Python 을 찾지 못했다.
echo  이 폴더의 install.bat 을 먼저 실행하고, 설치가 끝난 뒤 다시 실행할 것.
echo.
pause
exit /b 1

:findpy
REM Python 찾기: Python 관리자 / 사용자 설치 / PC 전체 설치 / Microsoft Store / py 실행기
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
exit /b 0
