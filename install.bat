@echo off
chcp 65001 >nul
title Safety Net 설치
cd /d "%~dp0"
echo.
echo  ==========================================
echo    Safety Net 설치
echo  ==========================================
echo.
if not exist data mkdir data

REM ---------------------------------------------------------- 1. Python 찾기
call :findpy
if defined PY goto havepy
echo  [1/4] Python 이 없어서 설치한다 (1~3분)...
winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements
call :findpy
if defined PY goto havepy
echo.
echo  Python 자동 설치에 실패했다.
echo  https://www.python.org/downloads/ 에서 Python 을 설치한 뒤
echo  (설치 첫 화면의 "Add python.exe to PATH" 체크) 이 파일을 다시 실행할 것.
pause
exit /b 1

:havepy
echo  [1/4] Python 확인: %PY%

REM ---------------------------------------------------------- 2. 패키지 설치
echo  [2/4] 필요한 패키지 설치 중 (1~2분)...
"%PY%" -m pip install --disable-pip-version-check -q -r requirements.txt
if errorlevel 1 (
  echo  패키지 설치 실패 - 인터넷 연결 확인 후 다시 실행할 것.
  pause
  exit /b 1
)

REM ---------------------------------------------------------- 3. 설정 파일 (이미 있으면 그대로)
if exist config.yaml (
  echo  [3/4] 기존 설정 유지 config.yaml
) else (
  if /i "%~1"=="me" (copy /y config.example.yaml config.yaml >nul) else (copy /y presets\other\config.yaml config.yaml >nul)
  echo  [3/4] 설정 파일 만듦 config.yaml
)

REM ---------------------------------------------------------- 4. 바탕화면 아이콘 + 실행
set "PYW=%PY:python.exe=pythonw.exe%"
> "data\pythonw.txt" echo %PYW%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Safety Net.lnk')); $s.TargetPath='%PYW%'; $s.Arguments='app.py'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0ui\img\reaper.ico'; $s.Save()"
echo  [4/4] 바탕화면에 "Safety Net" 아이콘을 만들었다.
echo.
echo  설치 완료 - Safety Net 을 시작한다. 처음 화면에서 거래소 키를 넣으면 된다.
start "" "%PYW%" app.py
timeout /t 4 >nul
exit /b 0

:findpy
set "PY="
if exist "%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe" set "PY=%LOCALAPPDATA%\Python\pythoncore-3.14-64\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
exit /b 0
