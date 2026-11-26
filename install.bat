@echo off
chcp 65001 >nul
title Safety Net 설치
cd /d "%~dp0"
if not exist data mkdir data
set "LOG=%~dp0data\install_log.txt"
> "%LOG%" echo [%date% %time%] Safety Net 설치 시작 - 폴더 %~dp0
echo.
echo  ==========================================
echo    Safety Net 설치  (기록: data\install_log.txt)
echo  ==========================================
echo.

REM ---------------------------------------------------------- 1. Python
call :findpy
if defined PY goto havepy
echo  [1/4] Python 이 없어서 설치한다 (1~3분)...
>> "%LOG%" echo Python 없음 - winget 으로 설치 시도
where winget >nul 2>nul
if errorlevel 1 (
  >> "%LOG%" echo winget 없음
  goto nopy
)
winget install -e --id Python.Python.3.13 --scope user --silent --accept-package-agreements --accept-source-agreements >> "%LOG%" 2>&1
call :findpy
if defined PY goto havepy
:nopy
>> "%LOG%" echo 실패: Python 을 찾지 못함
echo.
echo  [실패] Python 자동 설치가 안 됐다.
echo  1) https://www.python.org/downloads/ 에서 노란 Download 버튼으로 설치
echo     (설치 첫 화면 아래 "Add python.exe to PATH" 체크 후 Install Now)
echo  2) 설치가 끝나면 이 install.bat 을 다시 실행
echo.
pause
exit /b 1

:havepy
echo  [1/4] Python 확인: %PY%
>> "%LOG%" echo Python: %PY%
"%PY%" --version >> "%LOG%" 2>&1

REM ---------------------------------------------------------- 2. 패키지
echo  [2/4] 필요한 패키지 설치 중 (1~2분)...
"%PY%" -m pip install --disable-pip-version-check -r requirements.txt >> "%LOG%" 2>&1
if errorlevel 1 (
  >> "%LOG%" echo 실패: 패키지 설치
  echo.
  echo  [실패] 패키지 설치가 안 됐다. 인터넷 연결 확인 후 다시 실행할 것.
  echo  계속 안 되면 data\install_log.txt 파일을 보내줄 것.
  pause
  exit /b 1
)
"%PY%" -c "import webview, pystray, PIL, requests, yaml; print('imports ok')" >> "%LOG%" 2>&1
if errorlevel 1 (
  echo  [실패] 패키지 확인 실패 - data\install_log.txt 파일을 보내줄 것.
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
if not exist "%PYW%" set "PYW=%PY%"
> "data\pythonw.txt" echo %PYW%
>> "%LOG%" echo pythonw: %PYW%
powershell -NoProfile -ExecutionPolicy Bypass -Command "$d=[Environment]::GetFolderPath('Desktop'); $s=(New-Object -ComObject WScript.Shell).CreateShortcut((Join-Path $d 'Safety Net.lnk')); $s.TargetPath='%PYW%'; $s.Arguments='app.py'; $s.WorkingDirectory='%~dp0'; $s.IconLocation='%~dp0ui\img\reaper.ico'; $s.Save()" >> "%LOG%" 2>&1
echo  [4/4] 바탕화면에 "Safety Net" 아이콘을 만들었다.
>> "%LOG%" echo 설치 완료
echo.
echo  설치 완료 - Safety Net 을 시작한다. 처음 화면에서 거래소 키를 넣으면 된다.
start "" "%PYW%" app.py
timeout /t 5 >nul
exit /b 0

:findpy
REM Python 찾기: Python 관리자 / 사용자 설치 / PC 전체 설치 / Microsoft Store / py 실행기
set "PY="
for /d %%D in ("%LOCALAPPDATA%\Python\pythoncore-3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%ProgramFiles%\Python3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3*") do if exist "%%D\python.exe" set "PY=%%D\python.exe"
if not defined PY for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
exit /b 0
