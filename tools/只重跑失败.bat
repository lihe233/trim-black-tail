@echo off
setlocal
chcp 936 >nul
set "PYTHONIOENCODING=gbk"
title 只重跑失败的文件

echo ============================================================
echo    断点续跑（只重跑上次失败/中断的）
echo    作者：lihe233   B站主页：space.bilibili.com/267072796
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

rem 优先用发布包自带的便携 Python（解压即用，无需安装）
set "PY=%ROOT%\runtime\python\python.exe"
if not exist "%PY%" set "PY="
for /f "delims=" %%P in ('dir /b /s "%USERPROFILE%\.workbuddy\binaries\python\versions\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "C:/Python*/python.exe" 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [错误/Error] 没有可用的 Python / no usable Python found.
  echo   发布包自带 runtime\python\python.exe；若被删掉或只复制了 bat，请重新解压整个包。
  echo   也可以自己装 Python 3.10+ 并加进 PATH。
  goto :done
)

rem ===== ffmpeg / ffprobe：本机没有就先下载一次（约 60MB，只下一次）=====
set "NEEDFF="
if exist "%ROOT%\bin\ffmpeg.exe" if exist "%ROOT%\bin\ffprobe.exe" goto :haveff
where ffmpeg >nul 2>&1
if errorlevel 1 set "NEEDFF=1"
where ffprobe >nul 2>&1
if errorlevel 1 set "NEEDFF=1"
if not defined NEEDFF goto :haveff
echo.
echo 本机没找到 ffmpeg / ffprobe，视频处理要用它们。可以现在自动下载：
echo   约 60MB，存到工具目录的 bin 文件夹，只下一次，以后直接用。
echo   也可以自己装（winget install Gyan.FFmpeg），装完重新运行本 bat。
set /p "DL=现在下载？(Y/n，回车=下载): "
if /i "%DL%"=="n" (
  echo 已跳过下载，处理好 ffmpeg 后再运行。
  goto :done
)
"%PY%" "%SCRIPT%" --install-ffmpeg
if errorlevel 1 (
  echo.
  echo [错误] ffmpeg 下载失败。检查网络后重试，或手动下载解压到 bin 文件夹。
  goto :done
)
:haveff

rem 参数沿用主入口保存的 trim_config.ini
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
set "SKIPUND=N"
set "FLASH=black"
if exist "%CFG%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
)

set "SRC=%~1"
if not defined SRC set /p "SRC=请拖入上次处理过的文件夹（要续跑的那个）: "
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
  echo 请先用根目录的 01_开始裁剪_双击这里.bat 处理这个文件夹。
  goto :done
)

set "HEADARG="
if /i "%HEAD%"=="Y" set "HEADARG=--head"
set "SKIPARG="
if /i "%SKIPUND%"=="Y" set "SKIPARG=--skip-undetected"

echo.
echo 续跑：只处理记录表里状态为「失败」或「已中断」的文件
echo 文件夹：%SRC%
echo 记录表：%REPORT%
echo.

set "TRIM_LANG=zh"
if exist "%CFG%" for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do if /i "%%A"=="LANG" set "TRIM_LANG=%%B"
"%PY%" "%SCRIPT%" -i "%SRC%" -o "%OUT%" --retry-failed "%REPORT%" --scan-tail %SCAN% --workers %WORKERS% --dark-luma %LUMA% --curve-min-drop %DROP% --binary-levels %LEVELS% --core-threshold %CORE% --core-min-black %CMIN% --flash-gap %FGAP% --encoder %ENC% --prefilter-luma %PREFILTER% --flash %FLASH% %HEADARG% %SKIPARG% --report "%REPORT%"

:done
echo.
echo 已结束 / done. 按任意键关闭窗口...
pause >nul
