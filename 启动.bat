@echo off
REM =====================================================================
REM  pdf转 图形界面 —— 双击启动脚本
REM
REM  启动顺序（只找 pythonw.exe）：
REM    1. 程序目录\.venv\Scripts\pythonw.exe
REM    2. 程序目录\运行时\pythonw.exe
REM    3. PATH 里的 pythonw（where pythonw）
REM    4. D:\Python312\pythonw.exe
REM  找到后运行 工具\检查环境.py（控制台版，先问再装依赖），
REM  由它用 pythonw 拉起图形界面。
REM
REM  一个都找不到时开菜单：[1] 自动安装 [2] 手动安装 [3] 退出。
REM  本脚本自己绝不静默下载/安装 Python，自动安装也要先确认。
REM =====================================================================
chcp 936 >nul
setlocal EnableExtensions
cd /d "%~dp0"
set "APPDIR=%~dp0"
set "PY="
REM 本机测试：优先用已装好依赖的 venv
if exist "D:\新版本\.venv\Scripts\pythonw.exe" set "PY=D:\新版本\.venv\Scripts\pythonw.exe"


REM 1) 程序目录\.venv\Scripts\pythonw.exe
if exist "%APPDIR%.venv\Scripts\pythonw.exe" set "PY=%APPDIR%.venv\Scripts\pythonw.exe"
REM 2) 程序目录\运行时\pythonw.exe
if not defined PY if exist "%APPDIR%运行时\pythonw.exe" set "PY=%APPDIR%运行时\pythonw.exe"
REM 3) PATH 里的 pythonw
if not defined PY for /f "delims=" %%P in ('where pythonw 2^>nul') do if not defined PY set "PY=%%P"
REM 4) 固定位置 D:\Python312\pythonw.exe
if not defined PY if exist "D:\Python312\pythonw.exe" set "PY=D:\Python312\pythonw.exe"

if not defined PY goto :no_python

REM 测试钩子：只验证查找分支，不启动界面；正常双击不会设置这个变量。
if defined WHY_STARTUP_DRYRUN (
  echo [dryrun] PY=%PY%
  exit /b 0
)

set "CHECK=%APPDIR%工具\检查环境.py"
if not exist "%CHECK%" (
  echo.
  echo   找不到环境检查脚本：%CHECK%
  echo   直接打开图形界面……
  start "" "%PY%" -m why_harness gui %*
  exit /b 0
)

REM 控制台检查要用 python.exe；pythonw 没有控制台输出。
set "PYCON=%PY%"
for %%F in ("%PY%") do if /i "%%~nxF"=="pythonw.exe" set "PYCON=%%~dpFpython.exe"
if not exist "%PYCON%" set "PYCON=%PY%"
"%PYCON%" "%CHECK%" %*
exit /b %errorlevel%

:no_python
echo.
echo   没有找到 Python 3.12（pythonw.exe）。
echo   已找过：程序目录\.venv、程序目录\运行时、PATH、D:\Python312。
echo.
echo   [1] 自动安装（先确认，再下载到 %TEMP% 并运行安装程序）
echo   [2] 手动安装（显示下载网址和步骤）
echo   [3] 退出
echo.
choice /c 123 /n /m "请选择 [1/2/3]："
if errorlevel 3 goto :bye
if errorlevel 2 goto :manual
if errorlevel 1 goto :auto
goto :bye

:auto
echo.
echo   即将从 python.org 下载 Python 3.12.7 安装包：
echo   https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
echo   下载位置：%TEMP%\python-3.12.7-amd64.exe
choice /c YN /n /m "确认下载并安装？[Y/N]："
if errorlevel 2 goto :bye
set "INSTALLER=%TEMP%\python-3.12.7-amd64.exe"
echo   正在下载……
powershell -NoProfile -ExecutionPolicy Bypass -Command "Invoke-WebRequest -Uri 'https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe' -OutFile '%TEMP%\python-3.12.7-amd64.exe'"
if not exist "%INSTALLER%" goto :download_failed
echo   已下载，正在运行安装程序（约 1-2 分钟）……
start /wait "" "%INSTALLER%" /passive InstallAllUsers=0 PrependPath=1 Include_test=0
if exist "%INSTALLER%" del /q "%INSTALLER%" >nul 2>nul
echo.
echo   安装结束。若已装好，请重新双击 启动.bat。
pause
exit /b 0

:manual
echo.
echo   手动安装 Python 3.12.7：
echo   1. 打开 https://www.python.org/downloads/windows/
echo   2. 下载 Python 3.12.7 的 Windows installer (64-bit)：
echo      https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
echo   3. 安装时勾选 Add python.exe to PATH（可选 Install for all users）。
echo   4. 安装完成后重新双击 启动.bat。
echo.
pause
exit /b 0

:download_failed
echo.
echo   下载失败。请手动下载安装：
echo   https://www.python.org/ftp/python/3.12.7/python-3.12.7-amd64.exe
echo.
pause
exit /b 1

:bye
exit /b 0
