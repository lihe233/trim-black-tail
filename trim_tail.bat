@echo off
setlocal
chcp 936 >nul
set "PYTHONIOENCODING=gbk"

set "SCRIPT=%~dp0trim_black_tail.py"
if not exist "%SCRIPT%" (
  echo [错误/Error] 同目录下找不到 trim_black_tail.py / not found next to this bat.
  goto :done
)

set "PY="
for /f "delims=" %%P in ('dir /b /s "%USERPROFILE%\.workbuddy\binaries\python\versions\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "C:/Python*/python.exe" 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [错误] 找不到 Python，请安装 Python 3 或把 python.exe 加入 PATH。
  goto :done
)

set "SRC=%~1"
if not defined SRC (
  set /p "SRC=请把视频文件夹或文件拖到这里，然后回车："
)
if not defined SRC (
  echo [错误] 没有输入。
  goto :done
)
for %%I in ("%SRC%") do set "SRC=%%~fI"

set "SEC=%~2"
if not defined SEC (
  set /p "SEC=从每个视频结尾裁掉多少秒？例如 2.5 ："
)
if not defined SEC (
  echo [错误] 没有输入秒数。
  goto :done
)
set "SEC=%SEC: =%"

set "DRY=%~3"
if not defined DRY (
  set /p "DRY=只看清单不导出？[y/N]："
)
if /i "%DRY%"=="y" (
  set "DRY=--dry-run"
) else (
  set "DRY="
)

set "FAST=%~4"
if not defined FAST (
  set /p "FAST=快速流复制（裁剪点不够精确）？[y/N]："
)
if /i "%FAST%"=="y" (
  set "FAST=--copy"
) else (
  set "FAST="
)

set "OUT="
if exist "%SRC%\" set "OUT=%SRC%_no_tail"

echo.
if defined OUT (
set "TRIM_LANG=zh"
if exist "%~dp0trim_config.ini" for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0trim_config.ini") do if /i "%%A"=="LANG" set "TRIM_LANG=%%B"
  "%PY%" "%SCRIPT%" -i "%SRC%" -o "%OUT%" --tail %SEC% %DRY% %FAST%
) else (
  "%PY%" "%SCRIPT%" -i "%SRC%" --tail %SEC% %DRY% %FAST%
)

:done
echo.
if /i not "%~5"=="skip" pause
