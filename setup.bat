@echo off
REM Safety Net setup: install packages, run tests, check app modules. Result -> data\setup_log.txt
cd /d "%~dp0"
if not exist data mkdir data
set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not exist "%PY%" set "PY=python"
echo [%date% %time%] setup start > data\setup_log.txt
"%PY%" --version >> data\setup_log.txt 2>&1
"%PY%" -m pip install -r requirements.txt pytest >> data\setup_log.txt 2>&1
echo ---- TESTS ---- >> data\setup_log.txt
"%PY%" -m pytest -q >> data\setup_log.txt 2>&1
echo ---- IMPORTS ---- >> data\setup_log.txt
"%PY%" -c "import webview, pystray, PIL, clr; print('imports ok')" >> data\setup_log.txt 2>&1
echo SETUP DONE >> data\setup_log.txt
