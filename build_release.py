#!/usr/bin/env python3
"""打包发布 zip。

产物：dist/trim-black-tail-v<版本>.zip
内含顶层目录（解压不会散一地）：核心脚本、唯一入口 bat、tools 里的三个附加 bat、
中英文 README、一份 GBK 编码的使用说明。

用法：
    python build_release.py              # 构建到 dist/
    python build_release.py --out D:/x   # 指定输出目录
    python build_release.py --no-txt     # 不生成使用说明.txt
"""
import argparse
import os
import re
import sys
import urllib.request
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))

EXE_FILES = [
    "trim_black_tail.py",
    "01_开始裁剪_双击这里.bat",
    "tools/只扫描清单.bat",
    "tools/固定裁末尾N秒.bat",
    "tools/只重跑失败.bat",
]
DOC_FILES = [
    "README.md",
    "README.en.md",
]

# 便携版 Python（embeddable）：解压即用、不用安装、不用管理员权限，11MB。
# ffmpeg 太胖（两个 exe 四百多 MB）没法打进来，改成首次运行按需下载，见脚本注释。
PY_EMBED_VERSION = "3.13.10"
PY_EMBED_NAME = "python-%s-embed-amd64.zip" % PY_EMBED_VERSION
PY_EMBED_URL = ("https://registry.npmmirror.com/-/binary/python/%s/%s"
                % (PY_EMBED_VERSION, PY_EMBED_NAME))
PY_EMBED_MB = 11

README_TXT = r"""去视频片尾黑场 / 闪白 批量裁剪工具 v{version}
作者：{author}    B站主页：{bili}
仓库：{repo}

【怎么用】
1. 把整个压缩包解压到任意位置（别只把 bat 拖出来：
   旁边的 runtime\python 文件夹就是自带的 Python 运行环境）。
2. 双击  01_开始裁剪_双击这里.bat
   （只需要双击这一个，别的 bat 不用管。不用装 Python，包里已经带了）
3. 按提示把视频文件夹拖进窗口，回车。
   第一次会问你语言（中文 / 英文）、编码方式，直接回车用默认的就行。
4. 处理结果在源文件夹旁边的 xxx_no_black 子文件夹里，
   记录表是源文件夹里的 scan_trim_report.csv。

【第一次会下载 ffmpeg】
ffmpeg 没有打进压缩包（两个 exe 加起来四百多 MB，塞不进来）。
第一次运行时脚本会问一句「自动下载？」，直接回车就会下到本目录的 bin 文件夹里，
大约 60MB（只下一次）。电脑里已经装了 ffmpeg 的话会直接用，不会再下载。

【其它三个 bat（都在 tools 文件夹里，按需使用）】
  只扫描清单.bat        只看哪些视频会被裁、裁多少，不动文件
  固定裁末尾N秒.bat     不检测，每个视频统一裁掉末尾 N 秒
  只重跑失败.bat        上次失败的再来一遍

【片尾如果是黑底弹窗 / 系统提示框 / GAME OVER 字卡】
这种画面九成像素是黑的，但不是纯黑，默认会保留、不裁（算作剧情内容）。
想连它一起裁掉，就在命令行加 --dark-card cut。

【注意】
- ffmpeg 下载到本目录的 bin\ 里，之后一直用它；也能自己装好放进 PATH（自带的优先）。
- 想用显卡加速就装好 NVIDIA 驱动，脚本会自动检测 NVENC。
- 三个附加 bat 必须留在 tools 文件夹里，不能单独拖出去。

【常见问题】
详见 README.md（中文）/ README.en.md（English）。
"""


def read_version():
    path = os.path.join(ROOT, "trim_black_tail.py")
    with open(path, "r", encoding="utf-8") as f:
        m = re.search(r'^__version__\s*=\s*"([^"]+)"', f.read(), re.M)
    if not m:
        sys.exit("找不到 __version__，构建中止")
    return m.group(1)


def read_const(name):
    path = os.path.join(ROOT, "trim_black_tail.py")
    with open(path, "r", encoding="utf-8") as f:
        m = re.search(r'^%s\s*=\s*"([^"]*)"' % name, f.read(), re.M)
    return m.group(1) if m else ""


def check_bat(path):
    """bat 必须是 GBK 且全 CRLF，否则双击就是一闪而过或乱码。"""
    with open(path, "rb") as f:
        raw = f.read()
    name = os.path.relpath(path, ROOT)
    try:
        raw.decode("gbk")
    except UnicodeDecodeError as e:
        sys.exit("%s 不是 GBK 编码：%s" % (name, e))
    crlf, lf = raw.count(b"\r\n"), raw.count(b"\n")
    if crlf != lf:
        sys.exit("%s 存在裸 LF（CRLF %d / LF %d），构建中止" % (name, crlf, lf))
    return len(raw)


def fetch_embed_python(cache_dir):
    """下载或复用便携 Python 压缩包，返回本地路径。"""
    os.makedirs(cache_dir, exist_ok=True)
    dst = os.path.join(cache_dir, PY_EMBED_NAME)
    if os.path.isfile(dst) and os.path.getsize(dst) > 8 * 1024 * 1024:
        print("  = 复用缓存：%s" % PY_EMBED_NAME)
        return dst

    print("  下载便携 Python %s（约 %d MB）..." % (PY_EMBED_VERSION, PY_EMBED_MB))
    part = dst + ".part"
    req = urllib.request.Request(PY_EMBED_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
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
                    print("\r    %3d%%  %5.1f / %.1f MB"
                          % (pct, got / 1048576, total / 1048576), end="", flush=True)
    print("\r" + " " * 34 + "\r", end="", flush=True)
    os.replace(part, dst)
    return dst


def add_python_runtime(z, top, py_zip):
    """把便携 Python 的所有文件塞进 zip 的 runtime/python/ 下。"""
    base = top + "/runtime/python/"
    count = 0
    size = 0
    with zipfile.ZipFile(py_zip) as src:
        for info in src.infolist():
            if info.is_dir():
                continue
            name = info.filename.replace("\\", "/")
            with src.open(info) as fh:
                data = fh.read()
            z.writestr(base + name, data, zipfile.ZIP_DEFLATED)
            count += 1
            size += len(data)
    print("  + %-28s %7d B  (%d 个文件, %s)" % (
        "runtime/python/", size, count, PY_EMBED_VERSION))
    return count, size


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--no-txt", action="store_true")
    ap.add_argument("--no-python", action="store_true",
                    help="不打包便携 Python（包会退回 40KB，但用户得自己装 Python）")
    args = ap.parse_args()

    ver = read_version()
    top = "trim-black-tail-v%s" % ver
    os.makedirs(args.out, exist_ok=True)
    zip_path = os.path.join(args.out, top + ".zip")

    entries = []
    for rel in EXE_FILES + DOC_FILES:
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        if not os.path.isfile(p):
            sys.exit("缺少文件：%s" % rel)
        entries.append(rel)

    if not args.no_txt:
        txt = README_TXT.format(
            version=ver,
            author=read_const("__author__"),
            bili=read_const("__bilibili__"),
            repo=read_const("__repo__"),
        ).replace("\n", "\r\n")
        entries.append("使用说明.txt")

    py_zip = None
    if not args.no_python:
        py_zip = fetch_embed_python(os.path.join(args.out, ".cache"))

    total = 0
    n_files = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in entries:
            arc = top + "/" + rel
            if rel == "使用说明.txt":
                z.writestr(arc, txt.encode("gbk"))
                total += len(txt.encode("gbk"))
                n_files += 1
                print("  + %-28s %7d B  (GBK)" % (rel, len(txt.encode("gbk"))))
                continue
            src = os.path.join(ROOT, rel.replace("/", os.sep))
            if rel.endswith(".bat"):
                size = check_bat(src)
                z.write(src, arc)
                print("  + %-28s %7d B  (GBK+CRLF)" % (rel, size))
            else:
                z.write(src, arc)
                size = os.path.getsize(src)
                print("  + %-28s %7d B" % (rel, size))
            total += size
            n_files += 1
        if py_zip:
            n, sz = add_python_runtime(z, top, py_zip)
            n_files += n
            total += sz

    size = os.path.getsize(zip_path)
    print("\n构建完成：%s" % zip_path)
    print("  顶层目录：%s/" % top)
    print("  文件 %d 个，原始 %.1f MB，压缩后 %.2f MB"
          % (n_files, total / 1048576.0, size / 1048576.0))
    if size > 30 * 1024 * 1024:
        sys.exit("产物超过 30MB —— 八成是把 bin\\ 里的 ffmpeg 打进去了，删掉重来")

    # 复核：解压后 bat 仍是 CRLF，中文名没有乱码
    with zipfile.ZipFile(zip_path) as z:
        bad = [i.filename for i in z.infolist()
               if i.filename.endswith(".bat")
               and z.read(i.filename).count(b"\r\n") != z.read(i.filename).count(b"\n")]
        has_py = any(i.filename.endswith("/runtime/python/python.exe") for i in z.infolist())
    if bad:
        sys.exit("打包后 CRLF 校验失败：%s" % bad)
    print("  校验：%d 个 bat 均为 CRLF，中文路径正常" %
          len([e for e in entries if e.endswith(".bat")]))
    if py_zip:
        print("  校验：runtime/python/python.exe %s" % ("在包里" if has_py else "缺失！"))
        if not has_py:
            sys.exit("便携 Python 没打进去")


if __name__ == "__main__":
    main()
