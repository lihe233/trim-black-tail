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

README_TXT = """\
去视频片尾黑场 / 闪白 批量裁剪工具 v{version}
作者：{author}    B站：{bili}
仓库：{repo}

【怎么用】
1. 先确认电脑装了 Python 3.9+（命令行执行 python --version 能看到版本号）。
   没装就去 https://www.python.org/downloads/ 下，安装时勾选 Add Python to PATH。
2. 双击  01_开始裁剪_双击这里.bat
   （只需要双击这一个，别的 bat 不用管）
3. 按提示把视频文件夹拖进窗口，回车。
   第一次会问你语言（中文 / 英文）、编码方式，直接回车用默认的就行。
4. 处理结果在源文件夹旁边的 xxx_no_black 子文件夹里，
   记录表是源文件夹里的 tail_report.csv。

【其它三个 bat（都在 tools 文件夹里，按需使用）】
  只扫描清单.bat        只看哪些视频会被裁、裁多少，不动文件
  固定裁末尾N秒.bat     不检测，每个视频统一裁掉末尾 N 秒
  只重跑失败.bat        上次失败的再来一遍

【注意】
- 需要 ffmpeg。脚本会自动在 PATH 和常见目录里找，找不到会提示你放哪。
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.join(ROOT, "dist"))
    ap.add_argument("--no-txt", action="store_true")
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

    total = 0
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in entries:
            arc = top + "/" + rel
            if rel == "使用说明.txt":
                z.writestr(arc, txt.encode("gbk"))
                total += len(txt.encode("gbk"))
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

    size = os.path.getsize(zip_path)
    print("\n构建完成：%s" % zip_path)
    print("  顶层目录：%s/" % top)
    print("  文件 %d 个，原始 %d B，压缩后 %d B (%.1f KB)"
          % (len(entries), total, size, size / 1024.0))

    # 复核：解压后 bat 仍是 CRLF，中文名没有乱码
    with zipfile.ZipFile(zip_path) as z:
        bad = [i.filename for i in z.infolist()
               if i.filename.endswith(".bat")
               and z.read(i.filename).count(b"\r\n") != z.read(i.filename).count(b"\n")]
    if bad:
        sys.exit("打包后 CRLF 校验失败：%s" % bad)
    print("  校验：%d 个 bat 均为 CRLF，中文路径正常" %
          len([e for e in entries if e.endswith(".bat")]))


if __name__ == "__main__":
    main()
