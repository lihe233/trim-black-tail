@echo off
setlocal
chcp 936 >nul
set "PYTHONIOENCODING=gbk"

set "SCRIPT=%~dp0trim_black_tail.py"
if not exist "%SCRIPT%" (
  echo [错误/Error] 同目录下找不到 trim_black_tail.py / trim_black_tail.py not found next to this bat.
  goto :done
)

set "PY="
for /f "delims=" %%P in ('dir /b /s "%USERPROFILE%\.workbuddy\binaries\python\versions\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "%LOCALAPPDATA%\Programs\Python\python.exe" 2^>nul') do if not defined PY set "PY=%%P"
for /f "delims=" %%P in ('dir /b /s "C:/Python*/python.exe" 2^>nul') do if not defined PY set "PY=%%P"
if not defined PY set "PY=python"
"%PY%" --version >nul 2>&1
if errorlevel 1 (
  echo [错误/Error] 找不到 Python / Python not found - install Python 3 or add python.exe to PATH.
  goto :done
)

set "FIRST=%~1"
set "CFG=%~dp0trim_config.ini"
set "ASKED=0"
set "LOADED=0"

rem ===== 默认参数（首次运行用）=====
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
set "LANG=zh"

rem ===== 读取上次保存的配置 =====
if exist "%CFG%" (
  for /f "usebackq tokens=1,* delims==" %%A in ("%CFG%") do set "%%A=%%B"
  set "LOADED=1"
)

:loop
set "SRC="
if defined FIRST (
  set "SRC=%FIRST%"
  set "FIRST="
) else (
  echo.
  set /p "SRC=请拖入文件夹或粘贴路径（直接回车=退出）/ Drop a folder or paste a path (empty = quit): "
)
if not defined SRC goto :done
set "SRC=%SRC:"=%"
for %%I in ("%SRC%") do set "SRC=%%~fI"
if not exist "%SRC%\" (
  echo [错误/Error] 输入的不是文件夹 / not a folder: %SRC%
  goto :loop
)

set "OUT=%SRC%_no_black"
set "REPORT=%SRC%\scan_trim_report.csv"

rem ===== 参数只问一次：同一会话后续文件夹沿用 =====
if "%ASKED%"=="1" goto :run

if "%LOADED%"=="0" goto :ask
echo.
echo 上次配置 / Last settings：扫描 %SCAN%s / 线程 %WORKERS% / 亮度 %LUMA% / 降幅 %DROP% / 严格黑帧 %CORE%
echo     闪黑间隔 %FGAP%s / 编码 %ENC% / 初筛 %PREFILTER% / 片头 %HEAD% / 未检出复制 %SKIPUND% / 语言 %LANG%
echo     检测类型 %FLASH% (black=黑场 white=闪白 both=都检)
set /p "REUSE=沿用上次配置 / reuse last settings? (Y=yes / N=re-ask, default Y): "
if /i "%REUSE%"=="N" goto :ask
goto :saved

:ask
echo.
set /p "LANG=输出语言？（zh=中文 / en=English，默认 %LANG%）："
set /p "SCAN=只扫描末尾多少秒 / seconds to scan from the end? (默认 %SCAN%，0=全片): "
set /p "WORKERS=并发线程数 / worker threads? (默认 %WORKERS%): "
set /p "LUMA=暗场亮度阈值 / dark luma threshold? (默认 %LUMA%，0=关闭): "
set /p "DROP=淡出最小降幅 / min fade drop? (默认 %DROP%): "
set /p "LEVELS=二分加密层数 / bisection levels? (默认 %LEVELS%，0=均匀): "
set /p "CORE=严格黑帧阈值 / strict black threshold? (默认 %CORE%，越小越严): "
set /p "CMIN=最短连续黑帧秒数 / min continuous black? (默认 %CMIN%): "
set /p "FGAP=闪黑间隔容忍 / flash gap tolerance in seconds? (默认 %FGAP%): "
set /p "ENC=视频编码器 / video encoder? (默认 %ENC%=NVENC，libx264=CPU): "
set /p "PREFILTER=尾帧亮度初筛 / tail luma prefilter? (默认 %PREFILTER%，0=关闭): "
set /p "FLASH=检测哪种闪场 / which field? (black=黑场 white=闪白 both=都检，默认 %FLASH%): "
set /p "HEAD=也裁片头黑场 / also trim leading black? (Y=yes，默认 %HEAD%): "
set /p "SKIPUND=未检出的文件也复制到输出 / copy undetected files too? (Y=不复制，默认 %SKIPUND%=复制): "

:saved
set "ASKED=1"
> "%CFG%" (
  echo SCAN=%SCAN%
  echo WORKERS=%WORKERS%
  echo LUMA=%LUMA%
  echo DROP=%DROP%
  echo LEVELS=%LEVELS%
  echo CORE=%CORE%
  echo CMIN=%CMIN%
  echo FGAP=%FGAP%
  echo ENC=%ENC%
  echo PREFILTER=%PREFILTER%
  echo HEAD=%HEAD%
  echo SKIPUND=%SKIPUND%
  echo FLASH=%FLASH%
  echo LANG=%LANG%
)

:run
set "HEADARG="
set "SKIPARG="
if /i "%HEAD%"=="Y" set "HEADARG=--head"
if /i "%SKIPUND%"=="Y" set "SKIPARG=--skip-undetected"

echo.
echo 一步完成 / one shot：扫描片尾黑场 + 直接导出裁剪后的视频 (scan the tail then export).
echo 扫描范围 / scan：末尾 %SCAN% 秒，并发 %WORKERS% 线程
echo 淡出校验 / fade check：亮度 %LUMA%，最小降幅 %DROP%，二分加密 %LEVELS% 层
echo 严格黑帧 / strict black：阈值 %CORE%，最短 %CMIN% 秒，闪黑间隔 %FGAP% 秒
echo 视频编码 / encoder：%ENC% (auto = NVENC if available)
echo 尾帧初筛 / prefilter：亮度 %PREFILTER% 以上直接排除检测
echo 检测类型 / field：%FLASH% (black=片尾黑场 white=片尾闪白 both=两种都试)
if /i "%HEAD%"=="Y" echo 片头黑场 / leading black：开启（严格阈值 32/占比 95%%）
if /i "%SKIPUND%"=="Y" echo 未检出 / undetected：不复制到输出
echo 源文件夹 / input ：%SRC%
echo 输出文件夹 / output：%OUT%
echo 记录表 / report：%REPORT%
echo 原视频不会被修改 / source files are never modified.
echo 中途要停止 / to stop：在脚本同目录新建 STOP.txt，或直接关闭本窗口 (closing this window kills ffmpeg too).
echo.

set "TRIM_LANG=%LANG%"
"%PY%" "%SCRIPT%" -i "%SRC%" -o "%OUT%" --scan-tail %SCAN% --workers %WORKERS% --dark-luma %LUMA% --curve-min-drop %DROP% --binary-levels %LEVELS% --core-threshold %CORE% --core-min-black %CMIN% --flash-gap %FGAP% --encoder %ENC% --prefilter-luma %PREFILTER% --flash %FLASH% %HEADARG% %SKIPARG% --report "%REPORT%"

echo.
echo 本文件夹完成 / done：%OUT%
echo 有个别失败 / some failed? 运行 retry_failed.bat 拖入本文件夹即可只重跑失败的。
echo 可以继续输入下一个文件夹 / drop the next folder, or press Enter to quit.
goto :loop

:done
echo.
echo 已退出 / bye.
