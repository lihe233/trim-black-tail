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
  set /p "SRC=请把视频文件夹拖到这里，然后回车："
)
if not defined SRC (
  echo [错误] 没有输入文件夹。
  goto :done
)
for %%I in ("%SRC%") do set "SRC=%%~fI"
if not exist "%SRC%\" (
  echo [错误] 输入的不是文件夹。
  goto :done
)

set "WIN=%~2"
if not defined WIN (
  set /p "WIN=只扫描末尾 N 秒（0=全片扫描，最准）[0]："
)
if not defined WIN set "WIN=0"

set "REPORT=%SRC%\tail_report.csv"

echo.
echo 只检测片尾黑场，不导出任何视频。
set "TRIM_LANG=zh"
if exist "%~dp0trim_config.ini" for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0trim_config.ini") do if /i "%%A"=="LANG" set "TRIM_LANG=%%B"
"%PY%" "%SCRIPT%" -i "%SRC%" --dry-run --scan-tail %WIN% --report "%REPORT%"
echo.
echo 记录表：%REPORT%

:done
echo.
if /i not "%~3"=="skip" pause
