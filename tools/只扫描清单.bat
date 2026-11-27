@echo off
setlocal
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
title 只扫描清单

echo ============================================================
echo    只扫描清单（不出片，先看看会裁多少）
echo    作者：lihe233      B站：space.bilibili.com/267072796
echo    仓库：github.com/lihe233/trim-black-tail
echo ============================================================
echo.

for %%I in ("%~dp0..") do set "ROOT=%%~fI"
set "SCRIPT=%ROOT%\trim_black_tail.py"
set "CFG=%ROOT%\trim_config.ini"
if not exist "%SCRIPT%" (
  echo [错误/Error] 找不到 trim_black_tail.py，请把本文件放回 tools 子目录 / keep this file inside the tools folder.
  goto :done
)

set "PY="
for /f "delims=" %%P in ('dir /b /s "%USERPROFILE%\.workbuddy\binaries\python\versions\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "C:/Python*/python.exe" 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [错误/Error] 找不到 Python / Python not found.
  goto :done
)

set "SRC=%~1"
if not defined SRC (
  set /p "SRC=请把视频文件夹拖到这里，回车 / drop the folder here: "
)
if not defined SRC (
  echo [错误] 没有输入文件夹。
  goto :done
)
set "SRC=%SRC:"=%"
for %%I in ("%SRC%") do set "SRC=%%~fI"
if not exist "%SRC%\" (
  echo [错误] 输入的不是文件夹。
  goto :done
)

set "WIN=%~2"
if not defined WIN (
  set /p "WIN=只扫描末尾 N 秒（0=全片最准但最慢）[30]："
)
if not defined WIN set "WIN=30"

set "REPORT=%SRC%\tail_report.csv"

echo.
echo 只检测，不导出任何视频。结果写进 tail_report.csv
set "TRIM_LANG=zh"
if exist "%CFG%" for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do if /i "%%A"=="LANG" set "TRIM_LANG=%%B"
"%PY%" "%SCRIPT%" -i "%SRC%" --dry-run --scan-tail %WIN% --report "%REPORT%"
echo.
echo 记录表 / report：%REPORT%

:done
echo.
if /i not "%~3"=="skip" pause
