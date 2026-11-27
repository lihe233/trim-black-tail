@echo off
setlocal
chcp 936 >nul
set "PYTHONIOENCODING=gbk"

rem 只重跑上次记录表里失败/中断的文件，参数沿用 trim_config.ini
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

set "CFG=%~dp0trim_config.ini"
set "SCAN=30"
set "WORKERS=4"
set "LUMA=30"
set "DROP=20"
set "LEVELS=3"
set "CORE=64"
set "CMIN=0.15"
set "FGAP=0.5"
set "ENC=auto"
set "PREFILTER=45"
set "HEAD=N"
set "FLASH=black"
if exist "%CFG%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
)

set "SRC=%~1"
if not defined SRC set /p "SRC=请拖入上次处理过的文件夹（要续跑的那个）："
if not defined SRC goto :done
set "SRC=%SRC:"=%"
for %%I in ("%SRC%") do set "SRC=%%~fI"
if not exist "%SRC%\" (
  echo [错误] 输入的不是文件夹：%SRC%
  goto :done
)

set "OUT=%SRC%_no_black"
set "REPORT=%SRC%\scan_trim_report.csv"
if not exist "%REPORT%" (
  echo [错误] 没找到记录表：%REPORT%
  echo 请先运行 scan_and_trim.bat 处理这个文件夹。
  goto :done
)

set "HEADARG="
if /i "%HEAD%"=="Y" set "HEADARG=--head"

echo.
echo 续跑：只处理记录表里状态为「失败」或「已中断」的文件
echo 文件夹：%SRC%
echo 记录表：%REPORT%
echo.

set "TRIM_LANG=zh"
if exist "%~dp0trim_config.ini" for /f "usebackq tokens=1,* delims==" %%A in ("%~dp0trim_config.ini") do if /i "%%A"=="LANG" set "TRIM_LANG=%%B"
"%PY%" "%SCRIPT%" -i "%SRC%" -o "%OUT%" --retry-failed "%REPORT%" --scan-tail %SCAN% --workers %WORKERS% --dark-luma %LUMA% --curve-min-drop %DROP% --binary-levels %LEVELS% --core-threshold %CORE% --core-min-black %CMIN% --flash-gap %FGAP% --encoder %ENC% --prefilter-luma %PREFILTER% --flash %FLASH% %HEADARG% --report "%REPORT%"

:done
echo.
echo 已结束。按任意键关闭窗口...
pause >nul
