#!/usr/bin/env python3
"""批量去除视频结尾的黑场 / 闪白 / 淡出转场。

两种模式：
  auto   自动检测尾部连续黑场（或淡黑、闪白）并从起点裁掉
  fixed  不检测，直接从每个视频末尾裁掉固定的 N 秒

闪白 / 白场转场：`--flash white` 只测白场，`--flash both` 黑场白场都测
（取更早的起点）。实现上是先用 ffmpeg 的 negate 把白场反转成黑场，再复用
整套 blackframe 判定与曲线校验，阈值语义自动对称——原亮度 > 255-T 等价于
反转后 < T，所以「严格阈值 64」在两种极性下都是同样严格的。

auto 模式的异常与标记机制：
  - 异常：尾部检出的黑场时长接近扫描窗口（默认 >=80%），说明黑场可能
    比窗口更长、起点没找到 -> 自动改用全片重扫
  - 标记：最终裁剪量超过扫描窗口的一半（默认 >50%）-> 打标记，结尾汇总
    提醒人工复核
  - 所有异常 / 标记 / 偏暗待确认 / 失败 / 跳过在结尾统一列出
  - Windows 下直接关闭 cmd 窗口会触发清理，终止所有 ffmpeg 子进程

暗画面与闪黑处理：
  - 宽阈值（threshold=96）先扫出候选，再把候选附近交给严格黑帧检测
    （threshold=64、90% 像素低于阈值），把「偏暗剧情画面」和「真黑帧」分开
  - 严格黑帧允许短间隔（默认 0.5s），可把片尾闪黑的多段连成一段
  - 严格黑帧确认后，不再用亮度降幅/台阶否决（直接切黑、带字幕黑场都能过）

编码加速：`--encoder auto` 默认自动检测 NVIDIA NVENC（有则用 h264_nvenc，
没有则退回 libx264）；`--hw-cq` 控制 NVENC 质量（默认 22）。检测/黑帧扫描
仍是 CPU，GPU 只加速最终重编码；`--copy` 可直接流复制但不保证裁切点精确。

尾帧初筛：auto 模式先取最后一帧亮度，超过 `--prefilter-luma`（默认 45）就
跳过黑帧扫描、直接按“未检出”处理。样本库 141 个文件中，45 可零误伤跳过
36 个明显亮结尾；41 更激进（56 个），0 可关闭。

片头黑场：`--head` 开启后额外检测开头 `--head-scan`（默认 10）秒内延续到片头
的黑场，并用更严的 `--head-threshold`（默认 32）/`--head-amount`（默认 95）
复核，避免把夜景片头当黑场；检出后与片尾裁切一起应用。

断点续跑与筛选：
  - `--only "118,128"`：只处理文件名包含这些关键词的视频
  - `--retry-failed 上次报告.csv`：只重跑状态为失败/中断的文件

用法示例：
  python trim_black_tail.py -i D:\\素材 -o D:\\成片
  python trim_black_tail.py -i D:\\素材 -o D:\\成片 --scan-tail 5
  python trim_black_tail.py -i D:\\素材 -o D:\\成片 --tail 2
  python trim_black_tail.py -i D:\\素材 -o D:\\成片 --dry-run --report D:\\素材\\清单.csv

依赖：ffmpeg / ffprobe。缺失时会自动下载到本工具 bin\\ 下（约 60MB，一次），
      也会直接用 PATH 里已有的；Python 只用标准库。
"""

import argparse
import csv
import ctypes
import gzip
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

# ===== 项目信息（署名、仓库、主页，按需修改这几行即可）=====
__version__ = "1.5.0"
__author__ = "lihe233"
__repo__ = "https://github.com/lihe233/trim-black-tail"
__home__ = "https://github.com/lihe233"
__bilibili__ = "https://space.bilibili.com/267072796"   # 作者主页，引流用

# ===== 界面语言：zh=中文（默认），en=English =====
# 只影响控制台输出、帮助文本、错误提示；报告 CSV 的列名与状态值始终是中文，
# 因为断点续跑（--retry-failed）和结尾汇总靠这些字面值匹配，改了会失效。
LANG = "zh"


def H(zh: str, en: str) -> str:
    """按当前语言取文案：en 模式用英文，其余用中文。"""
    return en if LANG == "en" else zh


def _prescan_lang(argv: list[str]) -> str:
    """建 argparse 之前先扫一遍 --lang，好让 --help 本身也按语言输出。

    也可以用环境变量 TRIM_LANG=en 固定语言（bat 里就是这么传的）。
    """
    env = (os.environ.get("TRIM_LANG") or "").strip().lower()
    if env:
        return "en" if env.startswith("en") else "zh"
    for i, a in enumerate(argv):
        if a == "--lang" and i + 1 < len(argv):
            return "en" if argv[i + 1].lower().startswith("en") else "zh"
        if a.startswith("--lang="):
            return "en" if a.split("=", 1)[1].lower().startswith("en") else "zh"
    return "zh"


VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".flv", ".ts"}
BLACKFRAME_RE = re.compile(r"blackframe.*?\bt:([0-9]+\.?[0-9]*)")
YAVG_RE = re.compile(r"lavfi\.signalstats\.YAVG=([0-9]+\.?[0-9]*)")
FRAME_TIME_RE = re.compile(r"pts_time:([0-9]+(?:\.[0-9]+)?)")
STAT_RE = re.compile(r"lavfi\.signalstats\.(YAVG|YMAX|YMIN)=([0-9]+(?:\.[0-9]+)?)")
PRINT_LOCK = threading.Lock()
STOP = threading.Event()
PROCS: set[subprocess.Popen] = set()
PROCS_LOCK = threading.Lock()


def _request_stop(reason: str) -> None:
    if STOP.is_set():
        return
    STOP.set()
    with PROCS_LOCK:
        procs = list(PROCS)
    for proc in procs:
        try:
            proc.terminate()
        except OSError:
            pass
    # 控制台关闭回调必须立刻返回，超时强杀放到后台线程，避免 ffmpeg 忽略
    # terminate 时残留孤儿进程。
    threading.Thread(target=_kill_after_timeout, args=(procs,), daemon=True).start()
    log("[trim] " + H(f"{reason}，正在停止所有任务（已完成的视频保留）...",
              f"{reason}, stopping all tasks (finished videos are kept)..."))


def _kill_after_timeout(procs: list[subprocess.Popen], timeout: float = 3.0) -> None:
    for proc in procs:
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
            except OSError:
                pass


def _on_interrupt(signum, frame) -> None:
    """Ctrl+C：停止派发新任务，并杀掉所有正在运行的 ffmpeg 子进程。"""
    _request_stop(H("收到中断信号", "interrupt signal received"))


def _stop_file() -> Path:
    """停止哨兵文件：在脚本同目录建 STOP.txt 即可随时停止，Windows 下比 Ctrl+C 可靠。"""
    return Path(sys.argv[0]).resolve().parent / "STOP.txt"


def _watch_stop_file(sentry: Path) -> None:
    while not STOP.wait(0.5):
        if sentry.exists():
            _request_stop(H("检测到 STOP.txt", "STOP.txt detected"))
            return


signal.signal(signal.SIGINT, _on_interrupt)

# Windows：点 cmd 窗口右上角 X 会发 CTRL_CLOSE_EVENT，普通 SIGINT 捕获不到，
# 不处理的话 Python 被关闭后 ffmpeg 子进程会变成孤儿继续跑。这里显式接管。
if sys.platform == "win32":
    _KERNEL32 = ctypes.WinDLL("kernel32", use_last_error=True)

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)
    def _console_ctrl_handler(ctrl_type: int) -> bool:
        # CTRL_CLOSE_EVENT=2 / CTRL_LOGOFF_EVENT=5 / CTRL_SHUTDOWN_EVENT=6
        if ctrl_type in (2, 5, 6):
            _request_stop(H("控制台关闭", "console window closed"))
            return True
        return False

    _CONSOLE_CTRL_HANDLER = _console_ctrl_handler  # 必须保持引用，否则会被 GC
    _KERNEL32.SetConsoleCtrlHandler(_CONSOLE_CTRL_HANDLER, True)


def _decode(b: bytes | None) -> str:
    """ffmpeg 输出可能含任意字节（元数据、文件名等），宽松解码，绝不抛异常。"""
    if not b:
        return ""
    try:
        return b.decode("utf-8", errors="replace")
    except Exception:
        return b.decode("latin-1", errors="replace")


def run_proc(cmd: list[str], capture: bool = False) -> subprocess.CompletedProcess:
    """启动子进程并登记，便于 Ctrl+C 时统一终止。
    关键：管道必须按字节读（text=False），否则会继承 PYTHONIOENCODING=gbk，
    遇到 ffmpeg 输出中的非 GBK 字节时读取线程崩溃、结果变成 None。
    """
    r = subprocess.Popen(cmd, stdout=subprocess.PIPE if capture else None,
                         stderr=subprocess.PIPE if capture else None)
    with PROCS_LOCK:
        PROCS.add(r)
    try:
        if capture:
            out_b, err_b = r.communicate()
            out, err = _decode(out_b), _decode(err_b)
        else:
            r.wait()
            out, err = "", ""
        return subprocess.CompletedProcess(cmd, r.returncode, out, err)
    finally:
        with PROCS_LOCK:
            PROCS.discard(r)


def log(msg: str) -> None:
    with PRINT_LOCK:
        try:
            print(msg, flush=True)
        except UnicodeEncodeError:
            # GBK 终端遇到不可编码字符时降级输出，绝不中断流程
            try:
                print(str(msg).encode("gbk", errors="replace").decode("gbk"), flush=True)
            except Exception:
                pass  # 控制台正在关闭时输出会失败，绝不能影响清理流程
        except Exception:
            pass


def print_banner() -> None:
    """启动横幅：工具名、版本、作者、仓库与 B站 主页。"""
    bar = "=" * 52
    log(f"[trim] {bar}")
    log(f"[trim]   " + H("视频片尾黑场 / 闪白批量裁剪工具", "Video tail black / white trimmer")
        + f"   v{__version__}")
    log(f"[trim]   " + H(f"作者：{__author__}", f"Author: {__author__}"))
    if __repo__:
        log(f"[trim]   " + H(f"仓库：{__repo__}", f"Repo:   {__repo__}"))
    if __bilibili__:
        log(f"[trim]   " + H(f"B站主页：{__bilibili__}", f"Bili:   {__bilibili__}"))
    log(f"[trim]   " + H("原片不会被修改，输出目录默认 <文件夹名>_no_black",
                        "Source files are never modified; output goes to <folder>_no_black"))
    log(f"[trim] {bar}")


# ===== ffmpeg / ffprobe：定位 + 首次运行时自动下载 =====
# 二进制不随发布包分发：单个 ffmpeg.exe 就 100MB 以上，两个加起来 400MB+，
# 既压不小也过不了 GitHub 单文件 100MB 的限制。改成缺失时按需下载到
# <工具目录>\bin\，只解出需要的两个 exe，之后一直复用。
FFMPEG_URL = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
FFMPEG_MB = 60
NEED_BINS = ("ffmpeg", "ffprobe")

# 首选源：npmmirror 的 ffmpeg-static 二进制镜像（国内 CDN）。
# 两个 gz 各约 30MB，实测几十秒；比下 100MB+ 的整包快一个数量级。
# 版本写死 b6.1.1（ffmpeg 6.1.1 essentials 构建），已确认带 libx264 / libx265 /
# h264_nvenc / blackframe / signalstats，够本工具全部用法。要升级改这行路径即可。
NPMMIRROR_BASE = "https://registry.npmmirror.com/-/binary/ffmpeg-static/b6.1.1"
NPMMIRROR_FILES = {
    "ffmpeg": NPMMIRROR_BASE + "/ffmpeg-win32-x64.gz",
    "ffprobe": NPMMIRROR_BASE + "/ffprobe-win32-x64.gz",
}

# 备选源：static 构建（不能换 shared 版，那样只掏出 exe 会缺 dll）
FALLBACK_ZIPS = (
    ("BtbN/ghproxy",
     "https://ghproxy.net/https://github.com/BtbN/FFmpeg-Builds/releases/download/"
     "latest/ffmpeg-master-latest-win64-gpl.zip"),
    ("gyan.dev", FFMPEG_URL),
)


def tool_bin_dirs(explicit: str | None = None) -> list[Path]:
    """按优先级列出可能放 ffmpeg.exe / ffprobe.exe 的目录。"""
    dirs: list[Path] = []
    if explicit:
        dirs.append(Path(explicit).expanduser())
    here = Path(__file__).resolve().parent
    dirs.append(here / "bin")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            dirs.append(Path(local) / "trim-black-tail" / "bin")
    else:
        dirs.append(Path.home() / ".local" / "bin")
    return dirs


def find_tool(tool: str, dirs: list[Path]) -> str | None:
    """先看工具自带的 bin\\，再交给 PATH。"""
    names = [tool + ".exe", tool] if os.name == "nt" else [tool]
    for d in dirs:
        for n in names:
            p = d / n
            if p.is_file():
                return str(p)
    return shutil.which(tool)


def _manual_hint(missing: list[str]) -> str:
    dirs = tool_bin_dirs()
    main_dir = dirs[1] if len(dirs) > 2 else dirs[0]
    alt = dirs[-1]
    return H(
        f"找不到 {' / '.join(missing)}。三种办法：\n"
        f"  1) 让脚本自动下载（运行时直接回车即可）；\n"
        f"  2) 手动下载 {FFMPEG_URL}\n"
        f"     解压后把 bin\\ffmpeg.exe 和 bin\\ffprobe.exe 放进：{main_dir}\n"
        f"     （这个目录没有写权限的话，放这里也行：{alt}）\n"
        f"  3) 装好后把 ffmpeg 的 bin 目录加进系统 PATH。\n"
        f"     （Windows 也可以执行 winget install Gyan.FFmpeg）",
        f"{' / '.join(missing)} not found. Three options:\n"
        f"  1) Let this script download it (just press Enter);\n"
        f"  2) Download {FFMPEG_URL}\n"
        f"     and put bin\\ffmpeg.exe + bin\\ffprobe.exe into: {main_dir}\n"
        f"     (if that folder is read-only, use: {alt})\n"
        f"  3) Install ffmpeg and add its bin folder to PATH.\n"
        f"     (On Windows: winget install Gyan.FFmpeg)",
    )


def _interactive() -> bool:
    try:
        return sys.stdin is not None and sys.stdin.isatty()
    except Exception:
        return False


def _download_file(url: str, dst: Path, tag: str = "") -> None:
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (trim-black-tail)"})
    with urllib.request.urlopen(req, timeout=60) as r, open(dst, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        shown = -1
        while True:
            chunk = r.read(1 << 18)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                pct = got * 100 // total
                if pct != shown:
                    shown = pct
                    print(f"\r  {tag:16s} {pct:3d}%  {got / 1048576:5.1f} / "
                          f"{total / 1048576:.1f} MB", end="", flush=True)
    print("\r" + " " * 52 + "\r", end="", flush=True)


def _extract_bins(zip_path: Path, dest: Path) -> None:
    """只从压缩包里掏出 bin/ffmpeg.exe 与 bin/ffprobe.exe，其余不落盘。"""
    found: dict[str, zipfile.ZipInfo] = {}
    with zipfile.ZipFile(zip_path) as z:
        for info in z.infolist():
            norm = info.filename.replace("\\", "/").lower()
            base = norm.rsplit("/", 1)[-1]
            if base in ("ffmpeg.exe", "ffprobe.exe") and "/bin/" in norm and base not in found:
                found[base] = info
        missing = [n for n in ("ffmpeg.exe", "ffprobe.exe") if n not in found]
        if missing:
            raise RuntimeError(H(f"压缩包里没有 {'/'.join(missing)}，可能是下载不完整",
                                 f"{'/'.join(missing)} missing in the archive "
                                 f"- the download may be incomplete"))
        dest.mkdir(parents=True, exist_ok=True)
        for base, info in found.items():
            with z.open(info) as src, open(dest / base, "wb") as out:
                shutil.copyfileobj(src, out)


def _writable_install_dir(explicit: str | None = None) -> Path:
    cands: list[Path] = []
    if explicit:
        cands.append(Path(explicit).expanduser())
    cands.append(Path(__file__).resolve().parent / "bin")
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            cands.append(Path(local) / "trim-black-tail" / "bin")
    for d in cands:
        try:
            d.mkdir(parents=True, exist_ok=True)
            probe = d / ".write_test"
            probe.write_bytes(b"")
            probe.unlink()
            return d
        except OSError:
            continue
    raise RuntimeError(H(f"没有可写的目录来放 ffmpeg，请手动放到 {cands[0]}",
                         f"no writable folder for ffmpeg; put it in {cands[0]} manually"))


def _verify_installed(target: Path) -> tuple[str, str]:
    exe = ".exe" if os.name == "nt" else ""
    ffmpeg = target / ("ffmpeg" + exe)
    ffprobe = target / ("ffprobe" + exe)
    for p, what in ((ffmpeg, "ffmpeg"), (ffprobe, "ffprobe")):
        if not p.is_file():
            raise RuntimeError(H(f"下载后缺少 {what}", f"{what} missing after download"))
        r = run_proc([str(p), "-version"], capture=True)
        if r.returncode != 0:
            raise RuntimeError(H(
                f"{what} 跑不起来（退出码 {r.returncode}），可能下载不完整，"
                f"或系统缺少 VC++ 运行库",
                f"{what} cannot run (exit {r.returncode}); broken download or "
                f"missing VC++ runtime"))
    return str(ffmpeg), str(ffprobe)


def _install_from_mirror(target: Path) -> None:
    """npmmirror：两个 .gz 分别解出 ffmpeg.exe / ffprobe.exe。"""
    target.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as td:
        for i, tool in enumerate(NEED_BINS, 1):
            gz = Path(td) / (tool + ".gz")
            _download_file(NPMMIRROR_FILES[tool], gz,
                           tag="[%d/%d] %s" % (i, len(NEED_BINS), tool))
            with gzip.open(gz, "rb") as src, open(target / (tool + ".exe"), "wb") as out:
                shutil.copyfileobj(src, out)


def _install_from_zip(url: str, target: Path) -> None:
    with tempfile.TemporaryDirectory() as td:
        zp = Path(td) / "ffmpeg.zip"
        _download_file(url, zp, tag="zip")
        _extract_bins(zp, target)


def install_ffmpeg(target: Path) -> tuple[str, str]:
    print(f"[trim] " + H(f"正在下载 ffmpeg（约 {FFMPEG_MB} MB，只需一次）...",
                        f"Downloading ffmpeg (~{FFMPEG_MB} MB, one time)..."))
    if os.name != "nt":
        raise RuntimeError(H("非 Windows 系统请用包管理器安装：brew install ffmpeg / "
                             "sudo apt install ffmpeg",
                             "On non-Windows, install via your package manager: "
                             "brew install ffmpeg / sudo apt install ffmpeg"))

    plans: list[tuple[str, object]] = [("npmmirror", lambda: _install_from_mirror(target))]
    for label, url in FALLBACK_ZIPS:
        plans.append((label, lambda u=url: _install_from_zip(u, target)))

    errors: list[str] = []
    for label, fn in plans:
        try:
            fn()  # type: ignore[operator]
            ffmpeg, ffprobe = _verify_installed(target)
        except Exception as e:  # 网络失败、包结构变了、跑不起来，都换下一个源
            errors.append(f"{label}: {e}")
            print(f"[trim] " + H(f"{label} 不行，换下一个源…", f"{label} failed, trying next…"))
            continue
        print(f"[trim] " + H(f"安装完成（来源 {label}）：{target}",
                            f"installed from {label} into {target}"))
        return ffmpeg, ffprobe

    detail = "\n".join("  " + e for e in errors)
    raise RuntimeError(H(f"所有下载源都失败了：\n{detail}\n{_manual_hint(list(NEED_BINS))}",
                         f"every download source failed:\n{detail}\n"
                         f"{_manual_hint(list(NEED_BINS))}"))


def ensure_media_tools(args) -> tuple[str, str]:
    """返回 (ffmpeg, ffprobe) 的路径；缺了就（问一句后）自动装。"""
    dirs = tool_bin_dirs(getattr(args, "ffmpeg_dir", None))
    ffmpeg = find_tool("ffmpeg", dirs)
    ffprobe = find_tool("ffprobe", dirs)
    if ffmpeg and ffprobe:
        return ffmpeg, ffprobe

    missing = [n for n, p in zip(NEED_BINS, (ffmpeg, ffprobe)) if not p]
    if getattr(args, "no_download", False):
        raise RuntimeError(_manual_hint(missing))

    if not getattr(args, "install_ffmpeg", False):
        if not _interactive():
            raise RuntimeError(_manual_hint(missing))
        print(f"[trim] " + H(f"没有找到 {' / '.join(missing)}。",
                            f"{' / '.join(missing)} not found."))
        print(f"[trim] " + H(
            f"可以现在自动下载到工具目录（约 {FFMPEG_MB} MB，只下这一次），"
            f"也可以自己装（回车=下载）：",
            f"Download it now into the tool folder (~{FFMPEG_MB} MB, once), "
            f"or install it yourself (Enter = download):"))
        ans = input("[trim] " + H("自动下载？(Y/n) ", "Download now? (Y/n) ")).strip().lower()
        if ans in ("n", "no"):
            raise RuntimeError(_manual_hint(missing))

    target = _writable_install_dir(getattr(args, "ffmpeg_dir", None))
    return install_ffmpeg(target)


def resolve_encoder(encoder: str, ffmpeg: str) -> str:
    """auto 模式：本机有 NVENC 就用显卡，否则退回 CPU x264。"""
    if encoder != "auto":
        return encoder
    r = run_proc([ffmpeg, "-hide_banner", "-encoders"], capture=True)
    if "h264_nvenc" in (r.stdout or ""):
        return "h264_nvenc"
    return "libx264"


def probe_duration(path: Path, ffprobe: str) -> float:
    r = run_proc(
        [ffprobe, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            return float(r.stdout.strip())
        except ValueError:
            pass
    # 部分容器（MKV/TS/流）format.duration 是 N/A，退回取视频流时长
    r = run_proc(
        [ffprobe, "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture=True,
    )
    if r.returncode == 0 and r.stdout.strip():
        try:
            return float(r.stdout.strip().splitlines()[0])
        except (ValueError, IndexError):
            pass
    raise RuntimeError(H(f"无法读取时长：{path}", f"cannot read duration: {path}"))


def scan_black_times(path: Path, duration: float, args, ffmpeg: str,
                     scan_tail: float | None = None,
                     polarity: str = "black") -> list[float]:
    """返回暗帧时间点（秒，绝对时间）。scan_tail 可覆盖 args.scan_tail。

    polarity="white" 时先用 negate 把白场反成黑场，再复用同一套 blackframe
    判定——阈值语义自动对称（原亮度 > 255-T 等价于反转后 < T）。
    """
    window = args.scan_tail if scan_tail is None else scan_tail
    vf = f"blackframe=amount={args.amount}:threshold={args.threshold}"
    if polarity == "white":
        vf = "negate," + vf
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info"]
    if window > 0 and duration > window:
        # -copyts 保留原始时间戳，否则 -ss 跳转后 blackframe 报的是相对时间
        cmd += ["-copyts", "-ss", f"{duration - window:.3f}"]
    cmd += [
        "-i", str(path),
        "-vf", vf,
        "-an", "-f", "null", "-",
    ]
    if STOP.is_set():
        return []
    r = run_proc(cmd, capture=True)
    if STOP.is_set():
        return []
    if r.returncode != 0:
        raise RuntimeError(H(f"{'白场' if polarity == 'white' else '黑场'}检测失败：{path}",
                            f"{'white' if polarity == 'white' else 'black'} field detection failed: {path}"))
    times = [float(m) for m in BLACKFRAME_RE.findall(r.stderr)]
    return sorted(times)


def scan_core_black_times(path: Path, start: float, end: float,
                          args, ffmpeg: str,
                          polarity: str = "black") -> list[float]:
    """严格黑帧检测：把候选附近的「偏暗画面」与「真黑帧」分开。

    宽阈值 threshold=96 很容易把夜景/暗剧情画面也标成黑帧；这里改用更严的
    threshold=64（且仍要求 90% 像素低于阈值），只保留接近纯黑的真黑帧。
    polarity="white" 时同样先 negate，只保留接近纯白的帧。
    -ss/-t 放在 -i 之前并配合 -copyts，返回绝对时间戳。
    """
    start = max(0.0, start)
    span = max(0.0, end - start)
    if span <= 0 or STOP.is_set():
        return []
    vf = f"blackframe=amount={args.core_amount}:threshold={args.core_threshold}"
    if polarity == "white":
        vf = "negate," + vf
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info",
           "-copyts", "-ss", f"{start:.3f}", "-t", f"{span:.3f}",
           "-i", str(path),
           "-vf", vf,
           "-an", "-f", "null", "-"]
    r = run_proc(cmd, capture=True)
    if STOP.is_set() or r.returncode != 0:
        return []
    return sorted(float(m) for m in BLACKFRAME_RE.findall(r.stderr))


def sample_luma(path: Path, at: float, ffmpeg: str) -> float | None:
    """采样某一时刻画面的平均亮度 YAVG（0-255）。纯黑约 16，暗画面会明显更高。"""
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info",
           "-ss", f"{at:.3f}", "-i", str(path),
           "-vf", "signalstats,metadata=print:key=lavfi.signalstats.YAVG",
           "-frames:v", "1", "-an", "-f", "null", "-"]
    r = run_proc(cmd, capture=True)
    m = YAVG_RE.search(r.stderr or "")
    return float(m.group(1)) if m else None


def scan_frame_stats(path: Path, start: float, end: float,
                     ffmpeg: str) -> list[tuple[float, float, float, float]]:
    """逐帧返回 [(时间, YAVG, YMAX, YMIN), ...]，时间为绝对秒。

    YMAX 是整帧最亮像素：真纯黑整帧只有 16，而「黑底弹窗 / 片尾字卡」
    虽然九成像素也是黑的（YAVG 只有 20~35），文字和描边会把 YMAX 顶到 100 以上，
    所以 YMAX 是区分「纯黑」和「黑底有画面」最直接的指标。
    """
    span = max(0.0, end - start)
    if span <= 0 or STOP.is_set():
        return []
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-copyts",
           "-ss", f"{start:.3f}", "-t", f"{span:.3f}", "-i", str(path),
           "-vf", "signalstats,metadata=print:file=-", "-an", "-f", "null", "-"]
    r = run_proc(cmd, capture=True)
    if STOP.is_set() or r.returncode != 0:
        return []
    out: list[tuple[float, float, float, float]] = []
    cur: dict[str, float] = {}
    for line in (r.stdout or "").splitlines():
        m = FRAME_TIME_RE.search(line)
        if m:
            cur = {"t": float(m.group(1))}
            continue
        m = STAT_RE.search(line)
        if not m:
            continue
        cur[m.group(1)] = float(m.group(2))
        if m.group(1) == "YMAX" and "YAVG" in cur:
            out.append((cur["t"], cur["YAVG"], cur["YMAX"], cur.get("YMIN", 0.0)))
            cur = {}
    return out


def pure_field_start(frames, duration: float, args,
                     polarity: str = "black") -> float | None:
    """在逐帧统计里找「延续到片尾的纯净色场」起点；没有则返回 None。

    纯黑：整帧 YMAX 都很低（默认 <= 24）。纯白：整帧 YMIN 都很高。
    黑底弹窗 / 白底字卡因为有文字、描边、UI 边框，不满足这个条件。
    """
    if not frames or args.pure_ymax <= 0:
        return None
    cap = args.pure_ymax
    if polarity == "white":
        times = [f[0] for f in frames if f[3] >= 255.0 - cap]
    else:
        times = [f[0] for f in frames if f[2] <= cap]
    if not times:
        return None
    return trailing_black_start(times, duration, args,
                                gap_tol=args.gap_tol, min_black=0.0)


def dark_card_note(polarity: str = "black") -> str:
    """结尾是暗底（亮底）但有内容的画面时的说明文案。"""
    fname = "白" if polarity == "white" else "黑"
    return H(
        f"结尾画面是{'亮' if polarity == 'white' else '暗'}底但有内容"
        f"（整帧{'最暗' if polarity == 'white' else '最亮'}像素超出纯{fname}范围），"
        f"按片尾卡保留，不裁",
        f"the ending is a {'bright' if polarity == 'white' else 'dark'} card with "
        f"visible content, not pure {'white' if polarity == 'white' else 'black'} - kept")


def luma_curve(path: Path, start: float, end: float, ffmpeg: str,
               samples: int = 8, fps_hz: float = 0.0) -> list[float]:
    """在 [start, end] 采样平均亮度序列。
    fps_hz>0 时按该频率密集采样（供二分抽取用，一次 ffmpeg 调用）；
    否则按 samples 等间隔采样。
    """
    span = max(0.1, end - start)
    fps = fps_hz if fps_hz > 0 else max(0.5, samples / span)
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error",
           "-ss", f"{start:.3f}", "-i", str(path), "-t", f"{span:.3f}",
           "-vf", f"fps={fps:.3f},signalstats,metadata=print:"
                  f"key=lavfi.signalstats.YAVG:file=-",
           "-an", "-f", "null", "-"]
    r = run_proc(cmd, capture=True)
    vals = [float(m) for m in YAVG_RE.findall(r.stdout or "")]
    return vals


def binary_curve_points(dense: list[float], levels: int) -> tuple[list[float], list[int]]:
    """二分式抽取采样点：只对每个区间的后半继续二分，采样点向片尾指数加密。
    例：区间 [0,31]、levels=3 -> 索引 0,15,23,27,31
    这样片尾（黑场收敛段）采样最密，而亮部只需少量点做基准。
    返回 (按时间排序的亮度值, 对应索引)。
    """
    n = len(dense)
    if n < 3:
        return dense, list(range(n))
    idxs = {0, n - 1}
    lo, hi = 0, n - 1
    for _ in range(max(0, levels)):
        mid = (lo + hi) // 2
        if mid in idxs or mid <= lo or mid >= hi:
            break
        idxs.add(mid)
        lo = mid  # 关键：只细分后半段
    ordered = sorted(idxs)
    return [dense[i] for i in ordered], ordered


def fade_to_core(path: Path, broad_start: float, core_start: float,
                 ffmpeg: str, polarity: str = "black") -> bool:
    """判断宽阈值起点到严格起点之间是否是一路渐暗（白场则是渐亮）。

    真渐变：亮度连续下降，宽阈值起点应保留，避免少裁一段淡出。
    假黑场：亮度先稳在 50 左右、直到最后才掉到黑，应采用严格黑帧起点。
    白场把亮度取反（255-v）后复用同一判断。
    """
    if core_start <= broad_start:
        return False  # 严格黑帧更早，直接用更早的点
    span = min(core_start - broad_start + 0.2, 3.0)
    if span < 0.15:
        return False
    vals = luma_curve(path, max(0.0, broad_start - 0.1),
                      broad_start + span, ffmpeg,
                      # 白场渐变往往更陡，加密采样才不会因点数不足放弃保留宽起点
                      fps_hz=10 if polarity == "white" else 5)
    if len(vals) < 3:
        return False
    if polarity == "white":
        vals = [255.0 - v for v in vals]
    diffs = [a - b for a, b in zip(vals, vals[1:])]
    descending = sum(1 for d in diffs if d > 2)
    return (vals[0] - vals[-1] >= 10
            and descending >= max(1, int(len(diffs) * 0.6)))


def is_real_black(path: Path, black_start: float, duration: float, args, ffmpeg: str,
                  core_confirmed: bool = False, core_len: float = 0.0,
                  polarity: str = "black") -> tuple[bool, str]:
    """二次校验：用「渐变淡出」的形状特征确认是真黑场转场。

    采样策略：先密集采一条亮度曲线，再用二分法抽取判定点——每轮只细分
    后半段，采样点向片尾指数加密。这样过渡区（最关键的部分）点最密，
    快淡出也能抓到多个下降台阶，而亮部只需少量点做基准。

    判定（真淡出 vs 直接切暗画面）：
      1. 末值收敛到纯黑（≈16）；
      2. 首尾降幅足够；
      3. 二分采样序列里有多个下降台阶（渐变而非一步跳变）；
      4. 末段趋于稳定（画面变化渐渐变少）。

    polarity="white"（闪白/白场转场）时把采样亮度取反（255-v），
    上述规则全部原样复用，语义自动变成「收敛到纯白、升幅足够」。
    """
    is_white = polarity == "white"
    label = "白场" if is_white else "黑场"
    # 采样窗口锁定在黑场起点（渐变发生处）附近，而不是延伸到片尾：
    # 黑场可能长达数分钟，把窗口拉到片尾会让采样点全落在纯黑平台上，
    # 看不到开头的渐变。前 preroll 秒覆盖渐变起点，后 postroll 秒覆盖收敛段。
    curve_start = max(0.0, black_start - args.curve_preroll)
    curve_end = min(duration - 0.04, black_start + args.curve_postroll)
    curve_end = max(curve_end, curve_start + 0.2)
    span = curve_end - curve_start
    # 限制总点数，避免超长黑场拖慢速度
    fps = min(max(args.dense_fps, 2.0), max(2.0, 48.0 / span))
    dense = luma_curve(path, curve_start, curve_end, ffmpeg, fps_hz=fps)
    if len(dense) < 5:
        return True, H("亮度曲线采样不足，按检测结果处理",
                   "too few luma samples, trusting the coarse detection")
    if is_white:
        dense = [255.0 - v for v in dense]

    curve, idxs = binary_curve_points(dense, args.binary_levels)
    head = curve[0]
    tail = curve[-1]
    tail_min = min(curve[-2:])
    drop = head - tail_min

    # 下降台阶：二分采样序列中相邻点下降超过阈值
    diffs = [a - b for a, b in zip(curve, curve[1:])]
    steps = sum(1 for d in diffs if d > args.step_drop)
    max_step = max(diffs, default=0.0)
    # 「画面变化渐渐变少」用帧间变化量衡量，而不是亮度绝对波动——
    # 片尾字幕在黑底上闪现会让亮度波动很大，但帧间变化量整体是递减的。
    half = len(dense) // 2
    early_churn = sum(abs(a - b) for a, b in zip(dense[:half], dense[1:half])) / max(1, half - 1)
    late_churn = sum(abs(a - b) for a, b in zip(dense[half:], dense[half + 1:])) / max(1, len(dense) - half - 1)
    settle = max(dense[-4:]) - min(dense[-4:])  # 仅作说明，不作为否决依据

    detail = H(
        f"二分采样 {len(curve)} 点（末段加密）：" +
        ("反转亮度" if is_white else "亮度") +
        f" {head:.0f}->{tail:.0f}，"
        f"{'升幅' if is_white else '降幅'} {drop:.0f}、"
        f"{'上升' if is_white else '下降'}台阶 {steps}、末值 {tail_min:.0f}、"
        f"帧间变化 {early_churn:.1f}->{late_churn:.1f}",
        f"bisected {len(curve)} points (denser at the tail): " +
        ("inverted luma" if is_white else "luma") +
        f" {head:.0f}->{tail:.0f}, "
        f"{'rise' if is_white else 'drop'} {drop:.0f}, "
        f"steps {steps}, tail {tail_min:.0f}, "
        f"frame churn {early_churn:.1f}->{late_churn:.1f}")

    # 严格黑帧确认：90% 像素已低于 core_threshold，足以证明是真黑场。
    # 此时亮度降幅/台阶/末值只作记录，不再否决——否则「直接切黑」和
    # 「黑底带字幕导致均亮度偏高」都会被误判成偏暗画面。
    if core_confirmed:
        return True, H(
            f"严格{label}确认（阈值 {args.core_threshold}/占比 {args.core_amount}%）："
            f"窗口内连续{'白' if is_white else '黑'}帧 {core_len:.2f}s；" + detail,
            f"strict {'white' if is_white else 'black'} confirmed (threshold {args.core_threshold}/"
            f"share {args.core_amount}%): {core_len:.2f}s of continuous "
            f"{'white' if is_white else 'black'} frames; " + detail)

    # 规则1：末段必须收敛到纯黑（字幕会略微抬高末值，留 +6 容差）
    if args.dark_luma > 0 and tail_min > args.dark_luma + 6:
        return False, H(
            f"末值 {tail_min:.0f} 未收敛到{'纯白' if is_white else '纯黑'}，"
            f"疑似结尾本身偏{'亮' if is_white else '暗'}；" + detail,
            f"tail luma {tail_min:.0f} never settles to pure {'white' if is_white else 'black'} - "
            f"the ending itself looks {'bright' if is_white else 'dark'}; " + detail)

    # 规则2：整体降幅足够
    if drop < args.curve_min_drop:
        return False, H(
            f"{'升' if is_white else '降'}幅 {drop:.0f} 不足，"
            f"疑似直接切{'白' if is_white else '暗'}画面；" + detail,
            f"only {drop:.0f} luma change - looks like a hard cut to a "
            f"{'bright' if is_white else 'dark'} scene; " + detail)

    # 规则3：至少两个下降台阶；降幅极大时 1 个台阶也算（超快淡出）
    fast_fade = drop >= max(40.0, args.curve_min_drop * 2)
    if steps < 2 and not fast_fade:
        return False, H(
            f"仅 {steps} 个{'上升' if is_white else '下降'}台阶，无渐变特征；" + detail,
            f"only {steps} step(s) - no gradual fade signature; " + detail)

    # 帧间变化量仅作记录，不参与否决：
    # 采样区间前半常是稳定画面（变化小）、后半是淡出过程（变化大），
    # 强制"递减"会误伤真淡出；规则1~3 已经能区分淡出与直接切暗画面。
    return True, H(f"渐变{'淡入白场' if is_white else '淡出'}曲线确认：" + detail,
                   f"fade-{'in to white' if is_white else 'out'} curve confirmed: " + detail)


def trailing_black_start(times: list[float], duration: float, args,
                         gap_tol: float | None = None,
                         min_black: float | None = None) -> float | None:
    """找出一直延续到 duration 的连续暗场起点；找不到返回 None。

    gap_tol/min_black 可覆盖：严格黑帧检测用更大的闪黑间隔、更短的时长门槛。
    """
    if not times:
        return None
    gap_tol = args.gap_tol if gap_tol is None else gap_tol
    min_black = args.min_black if min_black is None else min_black
    if duration - times[-1] > args.end_tol:
        return None
    run_start = times[-1]
    for t in reversed(times[:-1]):
        if run_start - t <= gap_tol:
            run_start = t
        else:
            break
    if times[-1] - run_start + 0.04 < min_black:
        return None
    return max(0.0, run_start - args.pad_before)


def detect_tail_uniform(path: Path, duration: float, args, ffmpeg: str,
                        window: float, polarity: str = "black") -> dict:
    """检测延续到片尾的单一色场（黑场 / 白场），含初筛、异常重扫、严格复核。

    返回 {"start": 起点或 None, "anomaly": bool, "notes": [说明...],
          "reject": 否决原因或 None, "prefiltered": bool}
    start 为 None 表示未检出；reject 非空表示检出但因「结尾偏暗/偏亮」被否决。
    """
    label = "白场" if polarity == "white" else "黑场"
    out = {"start": None, "anomaly": False, "notes": [], "reject": None,
           "prefiltered": False}

    # 尾帧初筛：黑场看末帧够不够亮（亮则不可能有黑场）；
    # 白场反过来看末帧够不够白（按 255-v 折算，暗则不可能有白场）。
    if args.prefilter_luma > 0:
        tail_luma = sample_luma(path, max(0.0, duration - 0.15), ffmpeg)
        if tail_luma is not None:
            eff = tail_luma if polarity == "black" else 255.0 - tail_luma
            if eff > args.prefilter_luma:
                out["prefiltered"] = True
                out["notes"].append(H(
                    f"{label}初筛：末帧亮度 {tail_luma:.0f}"
                    + (f"（折算 {eff:.0f}）" if polarity == "white" else "")
                    + f" > {args.prefilter_luma:.0f}，跳过扫描",
                    f"{'white' if polarity == 'white' else 'black'} prefilter: tail luma "
                    f"{tail_luma:.0f}"
                    + (f" (mapped {eff:.0f})" if polarity == "white" else "")
                    + f" > {args.prefilter_luma:.0f}, scan skipped"))
                return out

    times = scan_black_times(path, duration, args, ffmpeg, polarity=polarity)
    start = trailing_black_start(times, duration, args)

    if start is not None and window > 0:
        detected = duration - start
        if detected >= window * args.anomaly_ratio:
            # 结尾是暗底弹窗/字幕卡时，全片重扫只会把起点推得更早，不会变出纯黑；
            # 先确认尾巴上到底有没有纯黑，没有就直接按片尾卡保留，省掉一次全片扫描。
            if args.dark_card == "keep" and args.pure_ymax > 0:
                fr = scan_frame_stats(path, start, duration, ffmpeg)
                if fr and pure_field_start(fr, duration, args, polarity) is None:
                    why = dark_card_note(polarity)
                    out["notes"].append(why)
                    out["card_keep"] = True
                    out["reject"] = why
                    out["start"] = start
                    return out
            # 异常：几乎占满窗口，起点可能在窗口之外 -> 全片重扫
            out["anomaly"] = True
            note = H(f"{label} {detected:.2f}s 接近窗口 {window:.2f}s，已自动全片重扫",
                     f"{'white' if polarity == 'white' else 'black'} field {detected:.2f}s "
                     f"nearly fills the {window:.2f}s window, whole file rescanned")
            times = scan_black_times(path, duration, args, ffmpeg,
                                     scan_tail=0, polarity=polarity)
            start = trailing_black_start(times, duration, args)
            if start is not None:
                note += H(f" -> 全片检出 {duration - start:.2f}s",
                          f" -> {duration - start:.2f}s over the whole file")
            out["notes"].append(note)
    if start is None:
        return out

    # 严格复核：宽阈值可能把夜景/过曝画面标成黑帧/白帧，用更严阈值另扫一遍
    broad = start
    core_confirmed = False
    core_len = 0.0
    cs = max(0.0, start - args.core_preroll)
    ce = min(duration, start + args.core_search)
    if ce <= cs:
        ce = duration
    core_times = scan_core_black_times(path, cs, ce, args, ffmpeg, polarity=polarity)
    if core_times:
        refined = trailing_black_start(core_times, ce, args,
                                       gap_tol=args.flash_gap,
                                       min_black=args.core_min_black)
        if refined is not None:
            core_confirmed = True
            core_len = ce - refined
            if fade_to_core(path, broad, refined, ffmpeg, polarity=polarity):
                out["notes"].append(H(
                    f"渐变起点 {broad:.3f}s 保留，严格{label} {refined:.3f}s",
                    f"kept the fade start {broad:.3f}s (strict {'white' if polarity == 'white' else 'black'} "
                    f"at {refined:.3f}s)"))
                start = broad
            else:
                out["notes"].append(H(
                    f"严格{label}起点 {refined:.3f}s，窗口内连续 {core_len:.2f}s",
                    f"strict start {refined:.3f}s, {core_len:.2f}s continuous in the window"))
                start = refined

    # 内容守卫：宽阈值与严格阈值都只看「暗像素占比」，而黑底弹窗、系统提示框、
    # GAME OVER 字卡这类片尾画面本来就有九成像素是黑的，两轮扫描都会放行。
    # 这里再看 YMAX（整帧最亮像素）：真黑场只有 16 左右，有文字/描边就会上到 100+。
    # 必须放在严格复核之后：严格复查会向前回溯 core_preroll 秒，把弹窗那一段
    # 也算成黑帧，只有在这里才能把裁切点拉回真正的纯黑起点。
    if args.dark_card == "keep" and args.pure_ymax > 0:
        frames = scan_frame_stats(path, start, duration, ffmpeg)
        pstart = pure_field_start(frames, duration, args, polarity)
        fname = "白" if polarity == "white" else "黑"
        if pstart is None:
            why = dark_card_note(polarity)
            out["notes"].append(why)
            out["card_keep"] = True
            out["reject"] = why
            out["start"] = start
            return out
        hold = pstart - start
        if hold > args.pure_gap_tol:
            out["notes"].append(H(
                f"起点 {start:.3f}s 到纯{fname}起点 {pstart:.3f}s 之间有 {hold:.2f}s 画面内容"
                f"（弹窗/字卡），裁切点后移到 {pstart:.3f}s",
                f"{hold:.2f}s of visible content between {start:.3f}s and the pure-{fname} "
                f"start {pstart:.3f}s (popup/caption card), cut moved to {pstart:.3f}s"))
            start = pstart
        elif pstart > start:
            out["notes"].append(H(
                f"纯{fname}起点 {pstart:.3f}s，与候选起点 {start:.3f}s 相差 "
                f"{hold:.2f}s（渐变尾巴），保留较早的起点",
                f"pure-{fname} start {pstart:.3f}s vs candidate {start:.3f}s "
                f"({hold:.2f}s apart, fade tail) - keeping the earlier start"))

    if args.dark_luma > 0:
        real, why = is_real_black(path, start, duration, args, ffmpeg,
                                  core_confirmed=core_confirmed,
                                  core_len=core_len, polarity=polarity)
        out["notes"].append(why)
        if not real:
            out["reject"] = why
            out["start"] = start
            return out
    out["start"] = start
    return out


def scan_head_black_times(path: Path, args, ffmpeg: str,
                          threshold: int | None = None,
                          amount: int | None = None) -> list[float]:
    """扫描片头 head_scan 秒内的暗帧时间点（时间戳从 0 开始）。"""
    window = max(0.0, args.head_scan)
    if window <= 0 or STOP.is_set():
        return []
    th = args.threshold if threshold is None else threshold
    am = args.amount if amount is None else amount
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "info",
           "-t", f"{window:.3f}", "-i", str(path),
           "-vf", f"blackframe=amount={am}:threshold={th}",
           "-an", "-f", "null", "-"]
    r = run_proc(cmd, capture=True)
    if STOP.is_set() or r.returncode != 0:
        return []
    return sorted(float(m) for m in BLACKFRAME_RE.findall(r.stderr))


def leading_black_end(times: list[float], duration: float, args,
                      gap_tol: float | None = None,
                      min_black: float | None = None) -> float | None:
    """找出从片头开始连续的暗场结束点；找不到返回 None。"""
    if not times:
        return None
    gap_tol = args.gap_tol if gap_tol is None else gap_tol
    min_black = args.min_black if min_black is None else min_black
    if times[0] > args.head_tol:
        return None
    run_end = times[0]
    for t in times[1:]:
        if t - run_end <= gap_tol:
            run_end = t
        else:
            break
    if run_end - times[0] + 0.04 < min_black:
        return None
    return min(duration, run_end + args.head_pad)


def detect_head_cut(path: Path, duration: float, args, ffmpeg: str) -> tuple[float, str] | None:
    """检测片头黑场，返回 (裁切点, 说明)；未检出返回 None。

    先用宽阈值找候选，再用严格黑帧复核确认。终点取两者更早的一个，
    避免把淡入画面的开头一起裁掉。
    """
    if not args.head or args.head_scan <= 0 or duration <= 0:
        return None
    wide = scan_head_black_times(path, args, ffmpeg)
    end = leading_black_end(wide, duration, args)
    if end is None:
        return None
    note = H(f"片头黑场到 {end:.2f}s", f"leading black until {end:.2f}s")
    core = scan_head_black_times(path, args, ffmpeg,
                                 threshold=args.head_threshold,
                                 amount=args.head_amount)
    core_end = leading_black_end(core, duration, args,
                                 gap_tol=args.flash_gap,
                                 min_black=args.core_min_black)
    # 必须通过严格黑帧复核：片头偏暗的夜景/片头渐入暗画面不能被当成黑场裁掉
    if core_end is None:
        return None
    end = min(end, core_end)
    note = H(f"片头黑场到 {end:.2f}s（严格黑帧复核）",
             f"leading black until {end:.2f}s (strict check passed)")
    if end <= 0.01:
        return None
    return end, note


def cut_video(src: Path, dst: Path, start_at: float, cut_at: float, args, ffmpeg: str) -> None:
    cmd = [ffmpeg, "-y", "-hide_banner", "-loglevel", "error"]
    if start_at > 0:
        # 输入 seek：重编码时默认精确 seek；--copy 时会落到关键帧，允许少量误差
        cmd += ["-ss", f"{start_at:.3f}"]
    cmd += ["-i", str(src), "-t", f"{max(0.0, cut_at - start_at):.3f}"]
    if args.copy:
        cmd += ["-c", "copy", "-avoid_negative_ts", "make_zero"]
    elif args.video_encoder in ("h264_nvenc", "hevc_nvenc"):
        # NVENC：p4 是速度/质量平衡点；cq 独立默认值 22，比 x264 crf18 稍快且体积接近
        cmd += ["-c:v", args.video_encoder, "-preset", args.hw_preset,
                "-cq", str(args.hw_cq), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k"]
    else:
        cmd += ["-c:v", "libx264", "-preset", args.preset, "-crf", str(args.crf),
                "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k"]
    cmd += ["-movflags", "+faststart", str(dst)]
    if STOP.is_set():
        raise RuntimeError("已中断")
    r = run_proc(cmd)
    if STOP.is_set() or r.returncode != 0:
        # 中断或失败时删除残缺输出，避免下次运行误判为「已完成」
        try:
            if dst.exists():
                dst.unlink()
        except OSError:
            pass
        raise RuntimeError("已中断" if STOP.is_set() else f"剪辑失败：{src}")


def main() -> None:
    global LANG
    # 先定语言，后面建 argparse 时帮助文本才是对的
    LANG = _prescan_lang(sys.argv[1:])
    p = argparse.ArgumentParser(
        description=H("批量去掉视频结尾黑场 / 淡出转场（多线程）",
                      "Batch-trim black / white / fade-out tails from videos (multi-threaded)"))
    p.add_argument("-i", "--input",
                   help=H("输入视频文件或目录", "Input video file or folder"))
    p.add_argument("-o", "--output",
                   help=H("输出目录（默认：<输入目录>_no_black）",
                          "Output folder (default: <input folder>_no_black)"))
    p.add_argument("--lang", choices=["zh", "en"], default=None,
                   help=H("输出语言：zh=中文（默认），en=English",
                          "Output language: zh=Chinese (default), en=English"))
    p.add_argument("--mode", choices=["auto", "fixed"], default="auto",
                   help=H("auto=自动检测片尾黑场；fixed=固定裁掉末尾 N 秒",
                          "auto=detect the tail colour field; fixed=cut a fixed N seconds"))
    p.add_argument("--seconds", type=float, default=2.0,
                   help=H("fixed 模式下裁掉的秒数", "Seconds to cut in fixed mode"))
    p.add_argument("--tail", type=float, default=None,
                   help=H("直接裁掉每个视频末尾 N 秒（等价于 --mode fixed --seconds N，不做黑场检测）",
                          "Cut the last N seconds of every video (same as --mode fixed --seconds N, no detection)"))
    p.add_argument("--threshold", type=int, default=96,
                   help=H("暗帧亮度阈值 0-255，越大越激进（默认 96，可试 128 抓淡出）",
                          "Dark frame luma threshold 0-255, higher = more aggressive (default 96, try 128 for fades)"))
    p.add_argument("--amount", type=int, default=90,
                   help=H("暗像素占比阈值 %%（默认 90）", "Share of dark pixels %% (default 90)"))
    p.add_argument("--min-black", type=float, default=0.3,
                   help=H("尾部暗场最短秒数（默认 0.3）", "Minimum tail duration in seconds (default 0.3)"))
    p.add_argument("--pad-before", type=float, default=0.0,
                   help=H("在检出的黑场起点前额外多裁秒数（用于去掉残留淡出）",
                          "Extra seconds cut before the detected start (removes leftover fade)"))
    p.add_argument("--scan-tail", type=float, default=30.0,
                   help=H("只检测末尾 N 秒（默认 30；0=全片逐帧最慢但最准）",
                          "Scan only the last N seconds (default 30; 0 = whole file, slowest but exact)"))
    p.add_argument("--flash", choices=["black", "white", "both"], default="black",
                   help=H("检测哪种色场：black=片尾黑场（默认）；white=片尾闪白/白场；both=两种都试，取更早的起点",
                          "Which field to detect: black=tail black (default); white=tail white flash; both=try both, keep the earlier start"))
    p.add_argument("--head", action="store_true",
                   help=H("同时检测并裁掉片头黑场（仅 auto 模式）",
                          "Also detect and cut a leading black field (auto mode only)"))
    p.add_argument("--head-scan", type=float, default=10.0,
                   help=H("片头检测范围：开头多少秒（默认 10）",
                          "Head scan range: how many seconds from the start (default 10)"))
    p.add_argument("--head-tol", type=float, default=0.4,
                   help=H("片头第一段黑场允许的起始延迟秒数（默认 0.4；超过则不算片头黑场）",
                          "Allowed delay before the first leading dark run in seconds (default 0.4)"))
    p.add_argument("--head-pad", type=float, default=0.05,
                   help=H("片头裁切点在黑场末端额外后移秒数（默认 0.05，确保黑场去干净）",
                          "Extra seconds after the leading dark run's end (default 0.05)"))
    p.add_argument("--head-threshold", type=int, default=32,
                   help=H("片头严格黑帧阈值 0-255（默认 32，比片尾更严；避免把夜景片头当黑场）",
                          "Leading strict black threshold 0-255 (default 32, stricter than the tail)"))
    p.add_argument("--head-amount", type=int, default=95,
                   help=H("片头严格黑帧暗像素占比 %%（默认 95）",
                          "Leading strict dark pixel share %% (default 95)"))
    p.add_argument("--anomaly-ratio", type=float, default=0.8,
                   help=H("检出黑场 >= 扫描窗口 * 该比例判为异常并自动全片重扫（默认 0.8）",
                          "Detected field >= window * this ratio counts as an anomaly and triggers a full rescan (default 0.8)"))
    p.add_argument("--flag-ratio", type=float, default=0.5,
                   help=H("裁剪量 > 扫描窗口 * 该比例则标记需复核（默认 0.5）",
                          "Cut amount > window * this ratio gets flagged for review (default 0.5)"))
    p.add_argument("--dark-luma", type=float, default=30.0,
                   help=H("淡出校验：末段亮度 > 该值+6 视为「结尾本身偏暗」，不裁（默认 30；0=关闭校验）",
                          "Fade check: tail luma > this + 6 means the ending is simply dark, no cut (default 30; 0=off)"))
    p.add_argument("--curve-min-drop", type=float, default=20.0,
                   help=H("淡出校验：亮度总降幅至少多少才认定为渐变淡出（默认 20）",
                          "Fade check: minimum total luma drop to count as a fade (default 20)"))
    p.add_argument("--dense-fps", type=float, default=6.0,
                   help=H("淡出校验：密集采样频率 点/秒（默认 6，二分抽取的母序列）",
                          "Fade check: dense sampling rate in points/s (default 6)"))
    p.add_argument("--binary-levels", type=int, default=3,
                   help=H("淡出校验：二分加密层数（默认 3；采样点向片尾指数加密，0=不加密）",
                          "Fade check: bisection levels toward the tail (default 3; 0=off)"))
    p.add_argument("--step-drop", type=float, default=3.0,
                   help=H("淡出校验：相邻采样点下降多少算一个台阶（默认 3）",
                          "Fade check: luma drop between samples that counts as a step (default 3)"))
    p.add_argument("--curve-preroll", type=float, default=1.0,
                   help=H("淡出校验：黑场起点前取多少秒覆盖渐变起点（默认 1）",
                          "Fade check: seconds before the start to cover the fade-in point (default 1)"))
    p.add_argument("--curve-postroll", type=float, default=2.0,
                   help=H("淡出校验：黑场起点后取多少秒覆盖收敛段（默认 2；长黑场不必采到片尾）",
                          "Fade check: seconds after the start to cover the settling part (default 2)"))
    p.add_argument("--dark-card", choices=["keep", "cut"], default="keep",
                   help=H("片尾黑底画面（系统弹窗/字卡/黑底字幕）怎么处理："
                          "keep=只裁真纯黑，有内容的画面保留（默认）；cut=当成黑场一起裁掉",
                          "How to treat a dark ending that still shows content "
                          "(system popup / caption card): keep = cut only pure black (default); "
                          "cut = treat it as black"))
    p.add_argument("--pure-ymax", type=float, default=24.0,
                   help=H("纯黑判定：整帧最亮像素 YMAX 不超过该值才算纯黑（默认 24；0=关闭该守卫）",
                          "Pure-black check: the brightest pixel YMAX must stay at or below this "
                          "value (default 24; 0 = off)"))
    p.add_argument("--pure-gap-tol", type=float, default=2.5,
                   help=H("候选起点到纯黑起点相差超过该秒数，说明中间是弹窗/字卡，裁切点后移（默认 2.5）",
                          "If the candidate start and the pure-black start differ by more than this, "
                          "the gap is treated as a popup and the cut moves forward (default 2.5)"))
    p.add_argument("--core-threshold", type=int, default=64,
                   help=H("严格黑帧阈值 0-255，越小越严（默认 64；排除偏暗剧情画面）",
                          "Strict black threshold 0-255, lower = stricter (default 64; rejects merely dark scenes)"))
    p.add_argument("--core-amount", type=int, default=90,
                   help=H("严格黑帧暗像素占比 %%（默认 90）", "Strict dark pixel share %% (default 90)"))
    p.add_argument("--core-min-black", type=float, default=0.15,
                   help=H("严格黑帧最短连续秒数（默认 0.15；可抓 0.2s 左右的直接切黑）",
                          "Minimum continuous strict-black seconds (default 0.15; catches ~0.2s hard cuts)"))
    p.add_argument("--core-preroll", type=float, default=1.5,
                   help=H("严格黑帧从候选起点向前回溯多少秒，覆盖闪黑第一段（默认 1.5）",
                          "Seconds to look back from the candidate start (default 1.5)"))
    p.add_argument("--core-search", type=float, default=15.0,
                   help=H("严格黑帧从候选起点向后最多找多少秒（默认 15；避免长黑场全片重扫）",
                          "Seconds to look forward from the candidate start (default 15)"))
    p.add_argument("--flash-gap", type=float, default=0.5,
                   help=H("严格黑帧之间允许的最大闪黑间隔（默认 0.5s；把多段闪黑连起来）",
                          "Maximum gap between flashes to join them (default 0.5s)"))
    p.add_argument("--prefilter-luma", type=float, default=45.0,
                   help=H("尾帧亮度初筛：末帧亮度大于该值则跳过黑帧扫描（默认 45；0=关闭）",
                          "Tail-luma prefilter: skip scanning when the last frame is brighter than this (default 45; 0=off)"))
    p.add_argument("--end-tol", type=float, default=0.3,
                   help=H("允许末尾几帧误差（默认 0.3）", "Tolerance for the last frames (default 0.3)"))
    p.add_argument("--gap-tol", type=float, default=0.2,
                   help=H("暗帧间允许的小间隔（默认 0.2）", "Allowed gap between dark frames (default 0.2)"))
    p.add_argument("--min-keep", type=float, default=1.0,
                   help=H("剪后至少保留秒数（默认 1）", "Minimum seconds kept after trimming (default 1)"))
    p.add_argument("--preset", default="veryfast",
                   help=H("x264 编码预设（默认 veryfast）", "x264 preset (default veryfast)"))
    p.add_argument("--crf", type=int, default=18,
                   help=H("x264 质量，越小越好（默认 18）", "x264 quality, lower = better (default 18)"))
    p.add_argument("--encoder", choices=["auto", "libx264", "h264_nvenc", "hevc_nvenc"],
                   default="auto",
                   help=H("视频编码器：auto=有 NVIDIA NVENC 就自动用 h264_nvenc，否则 libx264",
                          "Video encoder: auto=NVIDIA NVENC when available, otherwise libx264"))
    p.add_argument("--hw-preset", default="p4",
                   help=H("NVENC 预设 p1(最快)~p7(最慢)，默认 p4",
                          "NVENC preset p1(fastest)~p7(slowest), default p4"))
    p.add_argument("--hw-cq", type=int, default=22,
                   help=H("NVENC 质量 cq，越小越清晰越大（默认 22；x264 请用 --crf）",
                          "NVENC cq, lower = sharper and bigger (default 22; use --crf for x264)"))
    p.add_argument("--copy", action="store_true",
                   help=H("不重编码，速度快但裁剪点会有关键帧误差",
                          "Stream copy: fast, but the cut lands on a keyframe"))
    p.add_argument("--workers", type=int, default=4,
                   help=H("并发线程数（默认 4）", "Worker threads (default 4)"))
    p.add_argument("--dry-run", action="store_true",
                   help=H("只列清单不真正剪辑", "List results only, no actual encoding"))
    p.add_argument("--recursive", action="store_true",
                   help=H("递归扫描子目录", "Scan subfolders recursively"))
    p.add_argument("--overwrite", action="store_true",
                   help=H("覆盖已存在的输出文件", "Overwrite existing output files"))
    p.add_argument("--skip-undetected", action="store_true",
                   help=H("auto 模式下未检出片尾黑场的文件不输出（默认原样复制到输出目录）",
                          "In auto mode, do not output files with no tail field detected (default: copy as-is)"))
    p.add_argument("--only",
                   help=H("只处理文件名包含这些关键词的视频（逗号分隔，不区分大小写）",
                          "Only process files whose names contain these keywords (comma separated, case-insensitive)"))
    p.add_argument("--retry-failed", metavar="CSV",
                   help=H("读取上次报告，只重跑状态为失败/中断的文件（断点续跑）",
                          "Read the previous report and rerun only failed / interrupted files"))
    p.add_argument("--report",
                   help=H("把处理结果写成 CSV", "Write the results to a CSV"))
    p.add_argument("--ffmpeg-dir", metavar="DIR", default=None,
                   help=H("指定 ffmpeg.exe / ffprobe.exe 所在目录（默认先找本工具的 bin\\，再找 PATH）",
                          "Folder containing ffmpeg.exe / ffprobe.exe "
                          "(default: this tool's bin\\, then PATH)"))
    p.add_argument("--no-download", action="store_true",
                   help=H("缺少 ffmpeg 时不联网下载，直接报错退出（脚本/CI 用）",
                          "Never download ffmpeg automatically; fail instead (for scripts/CI)"))
    p.add_argument("--install-ffmpeg", action="store_true",
                   help=H("只下载安装 ffmpeg 到工具目录 bin\\ 然后退出，不处理视频",
                          "Only download ffmpeg into bin\\ and exit; process nothing"))
    args = p.parse_args()
    # 命令行显式给了就以它为准；没给则沿用 TRIM_LANG 环境变量（bat 走这条）
    if args.lang:
        LANG = args.lang
    if args.tail is not None:
        args.mode = "fixed"
        args.seconds = args.tail
    if args.workers < 1:
        args.workers = 1

    print_banner()

    try:
        ffmpeg, ffprobe = ensure_media_tools(args)
    except RuntimeError as e:
        print(f"[trim] {e}", file=sys.stderr)
        sys.exit(1)

    if args.install_ffmpeg:
        return
    if not args.input:
        print("[trim] " + H("缺少 -i 输入路径（或加 --install-ffmpeg 只装 ffmpeg）",
                            "missing -i input (or use --install-ffmpeg to just install ffmpeg)"),
              file=sys.stderr)
        sys.exit(2)

    args.video_encoder = resolve_encoder(args.encoder, ffmpeg)

    src = Path(args.input).expanduser().resolve()
    if not src.exists():
        print("[trim] " + H(f"输入不存在：{src}", f"input not found: {src}"), file=sys.stderr)
        sys.exit(1)

    src_is_dir = src.is_dir()
    if src_is_dir:
        out_dir = Path(args.output).expanduser().resolve() if args.output else src.parent / (src.name + "_no_black")
        files = sorted(src.rglob("*") if args.recursive else src.iterdir())
        files = [f for f in files if f.is_file() and f.suffix.lower() in VIDEO_EXTS]
        # 输出目录若嵌在输入目录内，递归时会把上次产物再次当输入处理
        if out_dir != src:
            files = [f for f in files
                     if not (f == out_dir or out_dir in f.parents)]
        out_root = out_dir
    else:
        files = [src]
        if args.output:
            cand = Path(args.output).expanduser().resolve()
            # 末尾带分隔符、已存在的目录、或没有扩展名，都按「输出目录」处理；
            # 否则当成输出文件名。否则 `-o 某目录` 会变成一个没有扩展名的文件，
            # ffmpeg 无法推断封装格式，直接报 Invalid argument。
            if cand.is_dir() or cand.suffix == "" or args.output.rstrip().endswith(("/", "\\")):
                out_dir = cand / (src.stem + ".mp4")
                out_root = cand
            else:
                out_dir, out_root = cand, cand.parent
        else:
            out_dir = src.parent / (src.stem + "_cut.mp4")
            out_root = out_dir.parent

    def rel_name(f: Path) -> str:
        """递归时带相对子路径，避免同名文件在报告和汇总里无法区分。"""
        if src_is_dir and args.recursive:
            try:
                return f.relative_to(src).as_posix()
            except ValueError:
                pass
        return f.name

    # 只处理指定文件（按文件名关键词，便于重跑个别素材）
    if args.only:
        keys = [k.strip().lower() for k in args.only.split(",") if k.strip()]
        if keys:
            files = [f for f in files
                     if any(k in rel_name(f).lower() for k in keys)]

    # 断点续跑：只挑上次报告里失败 / 中断的文件
    prev_rows: list[dict] = []
    if args.retry_failed:
        rp = Path(args.retry_failed).expanduser().resolve()
        if not rp.exists():
            print("[trim] " + H(f"找不到上次报告：{rp}", f"previous report not found: {rp}"), file=sys.stderr)
            sys.exit(1)
        with rp.open(encoding="utf-8-sig", newline="") as fh:
            old_rows = list(csv.DictReader(fh))
        prev_rows = old_rows
        bad = set()
        for old in old_rows:
            st = old.get("状态", "")
            if st.startswith("失败") or "已中断" in st:
                bad.add(old.get("文件", ""))
        files = [f for f in files if rel_name(f) in bad or f.name in bad]
        log("[trim] " + H(f"断点续跑：上次有 {len(bad)} 个失败/中断，本次匹配到 {len(files)} 个",
                       f"Resume: {len(bad)} failed/interrupted last time, {len(files)} matched now"))

    if not files:
        print("[trim] " + H(f"没有符合条件的视频：{src}", f"no matching videos in {src}"), file=sys.stderr)
        sys.exit(1)
    if not args.dry_run:
        out_root.mkdir(parents=True, exist_ok=True)

    def blank_row(f: Path) -> dict:
        return {
            "文件": rel_name(f), "原时长(s)": "", "模式": args.mode, "类型": "",
            "片头去掉(s)": "", "黑场起点(s)": "", "裁切点(s)": "", "去掉(s)": "",
            "末帧亮度": "", "状态": "", "输出": "",
            "异常重扫": "", "标记复核": "", "说明": "",
        }

    def process_one(f: Path, idx: int) -> dict:
        r = blank_row(f)
        if STOP.is_set():
            r["状态"] = "跳过：已中断"
            return r
        try:
            duration = probe_duration(f, ffprobe)
            r["原时长(s)"] = f"{duration:.3f}"
            black_start = None
            no_black = False
            cut_at = duration
            # 扫描窗口：窗口 >= 片长时等于全片扫描，不做异常/标记判定
            window = args.scan_tail if 0 < args.scan_tail < duration else 0.0

            # 片头黑场（仅 auto 模式）：与片尾独立检测，可单独裁掉
            head_cut = 0.0
            if args.mode == "auto" and args.head:
                head_hit = detect_head_cut(f, duration, args, ffmpeg)
                if head_hit is not None:
                    head_cut, head_note = head_hit
                    r["片头去掉(s)"] = f"{head_cut:.3f}"
                    r["说明"] = head_note

            if args.mode == "fixed":
                cut_at = duration - args.seconds
            else:
                # 末帧亮度始终采一次写入报告，便于复核初筛阈值是否合适
                tail_luma = sample_luma(f, max(0.0, duration - 0.15), ffmpeg)
                if tail_luma is not None:
                    r["末帧亮度"] = f"{tail_luma:.0f}"

                # 极性：黑场 / 白场（闪白）/ 两者都试
                pols = {"black": ["black"], "white": ["white"],
                        "both": ["black", "white"]}[args.flash]
                cands: list[tuple[float, str]] = []   # (起点, 极性)
                rejects: list[tuple[float, str, str]] = []  # (起点, 原因, 极性)
                card_keep: str | None = None   # 结尾是暗底有内容的画面，按片尾卡保留
                for pol in pols:
                    res = detect_tail_uniform(f, duration, args, ffmpeg, window, pol)
                    for nt in res["notes"]:
                        r["说明"] = (r["说明"] + H("；", "; ") if r["说明"] else "") + nt
                    if res.get("prefiltered"):
                        r["初筛"] = "是"
                    if res["anomaly"]:
                        r["异常重扫"] = "是"
                        log(f"[{idx}/{len(files)}] {r['文件']}: " + H("异常，已自动全片重扫", "anomaly - rescanned the whole file"))
                    if res["reject"]:
                        if res.get("card_keep") and not card_keep:
                            card_keep = res["reject"]
                        rejects.append((res["start"], res["reject"], pol))
                    elif res["start"] is not None:
                        cands.append((res["start"], pol))

                if cands:
                    # 多个极性都检出时取最早的起点（裁得更多），同起点黑场优先
                    cands.sort(key=lambda x: (x[0], 0 if x[1] == "black" else 1))
                    black_start, pol = cands[0]
                    cut_at = black_start
                    r["黑场起点(s)"] = f"{black_start:.3f}"
                    r["类型"] = "白场" if pol == "white" else "黑场"
                elif rejects:
                    # 检出了但被曲线校验否决：结尾本身偏暗/偏亮，不是转场
                    st, why, pol = sorted(rejects, key=lambda x: x[0])[0]
                    white = pol == "white"
                    if card_keep:
                        # 结尾是暗底弹窗/字卡（有内容），不是纯黑：整段保留
                        r["状态"] = "跳过：结尾是暗底画面（有内容，非纯黑）"
                        r["黑场起点(s)"] = f"{st:.3f}"
                        r["标记复核"] = "暗底待确认"
                        r["类型"] = "白场" if white else "黑场"
                        log(f"[{idx}/{len(files)}] {r['文件']}: {card_keep}")
                        return r
                    r["状态"] = ("跳过：疑似结尾偏亮（非白场）" if white
                                 else "跳过：疑似结尾偏暗（非黑场）")
                    r["黑场起点(s)"] = f"{st:.3f}"
                    r["标记复核"] = "偏亮待确认" if white else "偏暗待确认"
                    r["类型"] = "白场" if white else "黑场"
                    log(f"[{idx}/{len(files)}] {r['文件']}: {why}")
                    return r
                else:
                    if args.skip_undetected and head_cut <= 0:
                        r["状态"] = ("跳过：未检出片尾黑场/白场" if args.flash == "both"
                                     else "跳过：未检出片尾" +
                                     ("白场" if args.flash == "white" else "黑场"))
                        return r
                    no_black = True
                    cut_at = duration

                if not no_black and window > 0:
                    removed = duration - cut_at
                    if removed > window * args.flag_ratio:
                        r["标记复核"] = "是"
                        note = H(
                            f"裁剪 {removed:.2f}s 超过窗口 {window:.2f}s 的一半 "
                            f"{window * args.flag_ratio:.2f}s，需人工复核",
                            f"cut {removed:.2f}s exceeds half the {window:.2f}s window "
                            f"({window * args.flag_ratio:.2f}s) - worth a manual look")
                        r["说明"] = (r["说明"] + "；" if r["说明"] else "") + note

            has_cut = (not no_black) or head_cut > 0
            # 后两条只对「裁片尾」有意义：只裁片头时 cut_at 就是片尾本身，
            # 若也按这两条判，等于把「片头裁切」一律误判成裁切点不合理。
            bad_cut = cut_at - head_cut < args.min_keep
            if not no_black:
                bad_cut = bad_cut or cut_at >= duration - 0.05 or cut_at <= head_cut + 0.01
            if has_cut and bad_cut:
                r["状态"] = (f"跳过：裁切点 {cut_at:.2f}s 不合理"
                             + (f"（片头已去 {head_cut:.2f}s）" if head_cut > 0 else ""))
                r["裁切点(s)"] = f"{cut_at:.3f}"
                return r

            if src_is_dir:
                if args.recursive:
                    # 保留子目录结构，否则不同目录下的同名文件会互相覆盖
                    dst = out_root / f.relative_to(src).with_suffix(".mp4")
                else:
                    dst = out_root / (f.stem + ".mp4")
                dst.parent.mkdir(parents=True, exist_ok=True)
            else:
                dst = out_dir
            if dst.resolve() == f.resolve():
                raise RuntimeError("输出路径与输入文件相同，拒绝覆盖原片")

            removed = (duration - cut_at) + head_cut
            kept = cut_at - head_cut   # 保留长度要扣掉片头，否则只裁片头时会显示成整段时长
            head_txt = (H(f"片头去 {head_cut:.2f}s，", f"head -{head_cut:.2f}s, ") if head_cut > 0 else "")
            r["裁切点(s)"] = f"{cut_at:.3f}"
            r["去掉(s)"] = f"{removed:.3f}" if has_cut else ""

            field_zh = {'black': '黑场', 'white': '白场', 'both': '黑场/白场'}[args.flash]
            field_en = {'black': 'black tail', 'white': 'white tail', 'both': 'tail field'}[args.flash]

            if args.dry_run:
                if not has_cut:
                    r["状态"] = "试运行（原样复制）"
                    log(f"[{idx}/{len(files)}] {r['文件']}: "
                        + H(f"未检出{field_zh} -> 原样复制 [试运行]",
                            f"no {field_en} detected -> copied as-is [dry run]"))
                else:
                    r["状态"] = "试运行"
                    log(f"[{idx}/{len(files)}] {r['文件']}: {duration:.2f}s -> {head_txt}"
                        + H(f"保留 {kept:.2f}s (去掉 {removed:.2f}s) [试运行]",
                            f"kept {kept:.2f}s (removed {removed:.2f}s) [dry run]"))
                return r

            if dst.exists() and not args.overwrite:
                r["状态"] = "跳过：输出已存在"
                r["输出"] = str(dst)
                log(f"[{idx}/{len(files)}] {r['文件']}: " + H("输出已存在，跳过", "output exists, skipped"))
                return r

            if not has_cut:
                log(f"[{idx}/{len(files)}] {r['文件']}: "
                    + H(f"未检出{field_zh} -> 原样复制", f"no {field_en} detected -> copied as-is"))
                shutil.copy2(f, dst)
                r["状态"] = "完成（原样复制）"
            else:
                log(f"[{idx}/{len(files)}] {r['文件']}: {duration:.2f}s -> {head_txt}"
                    + H(f"保留 {kept:.2f}s (去掉 {removed:.2f}s)",
                        f"kept {kept:.2f}s (removed {removed:.2f}s)"))
                cut_video(f, dst, head_cut, cut_at, args, ffmpeg)
                r["状态"] = "完成"
            r["输出"] = str(dst)
            return r
        except Exception as e:
            if STOP.is_set():
                r["状态"] = "跳过：已中断"
            else:
                r["状态"] = "失败：" + str(e)
                log(f"[{idx}/{len(files)}] {r['文件']}: " + H(f"失败 {e}", f"failed: {e}"))
            return r

    flash_txt = H({"black": "片尾黑场", "white": "片尾闪白/白场",
                   "both": "片尾黑场+闪白"}[args.flash],
                  {"black": "tail black", "white": "tail white flash",
                   "both": "tail black + white"}[args.flash])
    if args.mode == "fixed":
        log("[trim] " + H(
            f"固定裁切：{len(files)} 个视频各去掉末尾 {args.seconds}s，并发 {args.workers} 线程，"
            f"视频编码 {args.video_encoder}"
            + ("（NVENC cq=%d）" % args.hw_cq if "nvenc" in args.video_encoder else ""),
            f"Fixed cut: {args.seconds}s off the end of {len(files)} videos, "
            f"{args.workers} workers, encoder {args.video_encoder}"
            + (" (NVENC cq=%d)" % args.hw_cq if "nvenc" in args.video_encoder else "")))
        results = [None] * len(files)
        sentry = _stop_file()
    else:
        log("[trim] " + H(
            f"开始处理 {len(files)} 个视频，检测 {flash_txt}，并发 {args.workers} 线程，"
            f"扫描窗口 {args.scan_tail}s，异常阈值 {args.anomaly_ratio}，标记阈值 {args.flag_ratio}，"
            f"严格黑帧阈值 {args.core_threshold}/{args.core_amount}%，"
            f"视频编码 {args.video_encoder}"
            + ("（NVENC cq=%d）" % args.hw_cq if "nvenc" in args.video_encoder else "")
            + ("，片头也裁黑场" if args.head else ""),
            f"Processing {len(files)} videos, detecting {flash_txt}, {args.workers} workers, "
            f"scan window {args.scan_tail}s, anomaly {args.anomaly_ratio}, flag {args.flag_ratio}, "
            f"strict black {args.core_threshold}/{args.core_amount}%, "
            f"encoder {args.video_encoder}"
            + (" (NVENC cq=%d)" % args.hw_cq if "nvenc" in args.video_encoder else "")
            + (", leading black too" if args.head else "")))
        results = [None] * len(files)
        sentry = _stop_file()
    if sentry.exists():
        sentry.unlink()
    threading.Thread(target=_watch_stop_file, args=(sentry,), daemon=True).start()
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futures = {}
        for i, f in enumerate(files, 1):
            if STOP.is_set() or sentry.exists():
                if not STOP.is_set():
                    _request_stop(H("检测到 STOP.txt", "STOP.txt detected"))
                break
            futures[ex.submit(process_one, f, i)] = i
        for fut in as_completed(futures):
            i = futures[fut]
            try:
                results[i - 1] = fut.result()
            except Exception as e:
                r = blank_row(files[i - 1])
                r["状态"] = "失败：" + str(e)
                results[i - 1] = r
    for i, r in enumerate(results):
        if r is None:
            r = blank_row(files[i])
            r["状态"] = "跳过：已中断"
            results[i] = r

    rows = [r for r in results if r is not None]
    done = sum(1 for r in rows if r["状态"].startswith("完成") or r["状态"].startswith("试运行"))
    skipped = sum(1 for r in rows if r["状态"].startswith("跳过"))
    failed = sum(1 for r in rows if r["状态"].startswith("失败"))
    anomalies = [r for r in rows if r["异常重扫"] == "是"]
    flagged = [r for r in rows if r["标记复核"] == "是"]
    dark_review = [r for r in rows if r["标记复核"] in ("偏暗待确认", "偏亮待确认")]
    prefiltered = [r for r in rows if r.get("初筛")]
    head_trimmed = [r for r in rows if r.get("片头去掉(s)")]

    if args.report:
        report = Path(args.report).expanduser().resolve()
        report.parent.mkdir(parents=True, exist_ok=True)
        with report.open("w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            cols = ["文件", "原时长(s)", "模式", "类型", "片头去掉(s)", "黑场起点(s)", "裁切点(s)",
                    "去掉(s)", "末帧亮度", "状态", "输出", "异常重扫", "标记复核", "说明"]
            out_rows = rows
            if prev_rows:
                # 断点续跑时不要把记录表覆盖成只剩重跑的几行：
                # 同名行用新结果，其余历史行原样保留，顺序不变
                fresh = {r["文件"]: r for r in rows}
                merged = []
                for old in prev_rows:
                    key = old.get("文件", "")
                    merged.append(fresh.pop(key, old))
                merged.extend(fresh.values())
                out_rows = merged
            w.writerow(cols)
            w.writerows([[r.get(k, "") for k in cols] for r in out_rows])
        log("[trim] " + H(f"清单：{report}", f"Report: {report}"))

    # ===== 结尾汇总 =====
    log("")
    log("[trim] ===== " + H("处理汇总", "Summary") + " =====")
    mode_txt = H("试运行", "Dry run") if args.dry_run else H("完成", "Done")
    interrupted = sum(1 for r in rows if "已中断" in r["状态"])
    log("[trim] " + H(
        f"{mode_txt} {done} 个，跳过 {skipped} 个，失败 {failed} 个，"
        f"异常重扫 {len(anomalies)} 个，标记复核 {len(flagged)} 个，"
        f"偏暗/偏亮待确认 {len(dark_review)} 个，初筛排除 {len(prefiltered)} 个"
        + (f"，片头裁切 {len(head_trimmed)} 个" if args.head else "")
        + (f"，因中断跳过 {interrupted} 个" if interrupted else ""),
        f"{mode_txt}: {done} done, {skipped} skipped, {failed} failed, "
        f"{len(anomalies)} rescanned, {len(flagged)} flagged, "
        f"{len(dark_review)} dark/bright to review, {len(prefiltered)} prefiltered"
        + (f", {len(head_trimmed)} head-trimmed" if args.head else "")
        + (f", {interrupted} skipped by interrupt" if interrupted else "")))
    if interrupted:
        log("[trim] " + H("提示：被中断的文件未处理，重新运行即可继续（已完成的会被跳过）",
                          "Note: interrupted files were untouched - run again and finished ones are skipped"))

    def section(title: str, items: list[dict], key: str) -> None:
        if not items:
            return
        log("[trim] --- " + title + H(f"（{len(items)}）---", f" ({len(items)}) ---"))
        for r in items:
            detail = r.get(key) or r["状态"]
            log(f"[trim]   {r['文件']}: {detail}")

    section(H("异常：尾部色场接近窗口上限，已自动全片重扫",
              "Anomaly: tail field nearly fills the window, whole file rescanned"), anomalies, "说明")
    section(H("标记：裁剪量超过设定窗口一半，需人工复核",
              "Flagged: cut exceeds half the window, needs a manual look"), flagged, "说明")
    section(H("偏暗/偏亮待确认：二次校验未通过，未裁剪，需人工看一眼",
              "Dark / bright to review: the second check failed, nothing was cut"), dark_review, "说明")
    section(H("失败", "Failed"), [r for r in rows if r["状态"].startswith("失败")], "状态")
    section(H("跳过", "Skipped"), [r for r in rows if r["状态"].startswith("跳过")], "状态")
    log("[trim] ===================")


if __name__ == "__main__":
    main()
