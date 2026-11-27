# Batch Trim Black Tails from Videos

Detects black tails, white flashes and fade-outs at the end of a video, cuts from the exact point where the transition starts, and exports a clean file. Your source files are never modified. Multi-threaded, GPU encoding, optional leading black, resumable runs.

Built for batch editing: a whole season of footage each ends with seconds to minutes of black, a fade to black or a white flash — trimming them one by one is unbearable, while this tool does a whole folder in one go.

> **Author**: GitHub [@lihe233](https://github.com/lihe233) ｜ My **Bilibili profile**: [space.bilibili.com/267072796](https://space.bilibili.com/267072796) ｜ [Repository](https://github.com/lihe233/trim-black-tail)

[中文](README.md) ｜ **English**

## Features

- **Only cuts a field that runs to the very end** — black or white transitions in the middle of a video are left alone
- **Black and white both supported**: `--flash black|white|both`; white uses ffmpeg `negate` and reuses the same detection
- **Two-stage decision**: a wide scan first, then a strict re-check that separates merely dark (or blown-out) footage from a real field
- **Fade-aware**: a gradual fade to black (or fade to white) is cut from where the fade begins, not only where it becomes pure black
- **Catches short flashes**: several 0.2–1 s flashes at the end are joined and cut as one segment
- **Handles very long fields**: a multi-minute black tail triggers a whole-file rescan to find the real start
- **Tail-luma prefilter**: files whose last frame is still bright (black mode) or already very dark (white mode) skip the scan entirely
- **GPU encoding**: uses NVIDIA NVENC automatically when available, roughly 2× faster than CPU
- **Leading black** (optional) with a stricter threshold, so a dark night scene at the start is not mistaken for a field
- **Resumable**: rerun only the files that failed or were interrupted
- **Bilingual output**: `--lang en` switches the console to English; the .bat menus can too — see [Language](#language)
- **One obvious entry point**: a single `01_开始裁剪_双击这里.bat` in the root, all other tools under `tools\`
- **Non-destructive**: outputs go to `<folder>_no_black` plus a CSV report

## Requirements

- Python 3.8+ (**the Windows release bundles a portable 3.13** — nothing to install; only needed if you run the source yourself), standard library only, no `pip install`
- **ffmpeg / ffprobe**: no manual install. On first run, if they are missing, the script offers to download them into the tool's `bin\` folder (~120MB, one time). If you prefer your own copy, put it on `PATH` — the bundled `bin\` wins, `PATH` is the fallback.
- Optional: an NVIDIA GPU and a recent ffmpeg for `h264_nvenc` export

> Why the binaries are not shipped inside the zip: the two exes total 430MB+, and the archive would still be far over GitHub's 100MB per-file limit. A one-time download is the better trade.

## Quick start

### Windows GUI entry points

> **Not sure what to click after downloading? Click `01_开始裁剪_双击这里.bat`.** It is the only entry point in the root folder; every other tool lives in `tools\` and can be ignored.

> The release ships with a portable Python (`runtime\python`, no install needed) and downloads ffmpeg on demand, so **unzip and run — no environment setup at all**. The only prompt on first run is a single "download ffmpeg?" — press Enter.

Double-click it, drag a folder into the window, press Enter, then answer the prompts (pressing Enter on every prompt uses the defaults). When it finishes you can drop the next folder in.

The window shows the tool name, author, Bilibili and repository address before it asks for anything — so you can see what you are running first.

Settings are saved to `trim_config.ini`, so the next run only asks “reuse last settings?” — press Enter.

What is in the folder:

```
01_开始裁剪_双击这里.bat    the only entry point: scan + trim in one go
trim_black_tail.py          the engine (the .bat finds Python and calls it)
tools\
  只扫描清单.bat            scan only, writes tail_report.csv
  固定裁末尾N秒.bat         cut a fixed N seconds, no detection
  只重跑失败.bat            resume: rerun the failed / interrupted files
```

The three tools under `tools\` locate the engine and config through `..`, so do not move them out on their own (moving the whole folder is fine).

### Command line

```bash
# Detect the tail field and trim, output to D:\out
python trim_black_tail.py -i D:\footage -o D:\out

# List what would happen, no encoding
python trim_black_tail.py -i D:\footage -o D:\out --dry-run --report report.csv

# No detection, just cut the last 3 seconds
python trim_black_tail.py -i D:\footage --tail 3

# Also trim leading black, 8 workers, recurse into subfolders
python trim_black_tail.py -i D:\footage --head --workers 8 --recursive

# White flash / white tail (the default output folder is still _no_black)
python trim_black_tail.py -i D:\footage --flash white

# Detect both, keep whichever starts earlier
python trim_black_tail.py -i D:\footage --flash both

# Only these files
python trim_black_tail.py -i D:\footage --only "118,128,143"

# Rerun only what failed or was interrupted last time
python trim_black_tail.py -i D:\footage --retry-failed report.csv
```

## Language

Console output, `--help` text and error messages are available in Chinese and English. Chinese is the default.

| How | Usage |
|---|---|
| CLI flag | `python trim_black_tail.py -i D:\footage --lang en` |
| Environment variable | `set TRIM_LANG=en` (this is what the .bat launchers use) |
| .bat menu | The first question is “输出语言？（zh=中文 / en=English）”; the answer is stored in `trim_config.ini` and reused |

Priority: `--lang` > `TRIM_LANG` > `zh`.

Two notes:

- **CSV column names and status values stay in Chinese** (`完成` / `跳过` / `失败`, …). Resume logic and the summary match on those literal values, so translating them would break `--retry-failed`. Only human-facing output is translated; the data is not.
- The .bat prompts show **Chinese and English on the same line** (e.g. `只扫描末尾多少秒 / seconds to scan from the end?`) and do not follow the language setting — that way neither audience has to guess, and there is no branching that could break the menu.

## How it works

### 1. Tail-luma prefilter (optional, on by default)

Read the luma (YAVG) of the last frame. If it is above `--prefilter-luma` (default 45) the ending is still bright, so there cannot be a black field — the scan is skipped. About 0.16 s per file.

### 2. Wide scan

Run `blackframe=amount=90:threshold=96` over the last `--scan-tail` seconds (default 30), collect every dark frame timestamp, and take the run that continues to the very end as the candidate.

### 3. Strict re-check

The wide threshold also flags merely dark scenes, so the area around the candidate start is scanned again with `threshold=64` and 90 % of pixels below it, keeping only near-pure black frames:

- gaps up to `--flash-gap` (default 0.5 s) are bridged, joining several flashes into one segment
- the minimum continuous run drops to `--core-min-black` (default 0.15 s), which catches a hard cut to black
- if the stretch between the candidate and the first strict black frame is a continuous fade, the earlier start is kept so the fade is not left half-cut

### 4. Luma curve check

A luma curve is sampled around the start (1 s before, 2 s after, bisected so samples get denser toward the tail) to tell a fade-out from genuinely dark footage. It also records the tail luma, the total drop and the number of steps into the report.

> Once the strict check passes, the curve is only recorded, never used to veto the cut — that is what makes hard cuts and black-with-subtitles tails work.

### White flash / white tail

The principle is fully symmetric, so no second detector is needed: ffmpeg `negate` inverts the picture, which turns a white field into a black one, and the same scan, strict re-check and curve check apply.

Threshold semantics stay symmetric after inversion — “original luma > 255−T” equals “inverted < T” — so a single `--core-threshold 64` is equally strict for both polarities (pure black < 64, pure white > 191, both requiring 90 % of pixels).

| `--flash` | Behaviour |
|---|---|
| `black` (default) | Black tail only |
| `white` | White flash / white tail only |
| `both` | Both, keeping whichever start is earlier |

The prefilter is symmetric too: black mode asks whether the last frame is bright enough, white mode whether it is white enough (mapped through `255−v`), so one `--prefilter-luma 45` fits both.

### 5. Anomalies and review

- **Rescan**: when the detected field nearly fills the scan window (≥ 80 % by default) it may be longer than the window, so the whole file is rescanned
- **Flagged**: a cut larger than half the window (> 50 % by default) is flagged in the summary for a manual look
- **Dark/bright to review**: when the curve check shows the ending is simply dark (or bright), nothing is cut and the file is listed separately

## Common options

| Option | Default | Meaning |
|---|---|---|
| `-i, --input` | required | Input video file or folder |
| `-o, --output` | `<input>_no_black` | Output folder |
| `--mode` | `auto` | `auto` detects the field, `fixed` cuts a fixed length |
| `--seconds` | — | Seconds to cut in `fixed` mode |
| `--tail` | — | Same as `--mode fixed --seconds N` |
| `--scan-tail` | 30 | Scan only the last N seconds, 0 = whole file |
| `--flash` | `black` | Which field: `black` / `white` / `both` |
| `--core-threshold` | 64 | Strict threshold, lower = stricter (black < 64, white > 191) |
| `--core-min-black` | 0.15 | Minimum continuous strict field in seconds |
| `--flash-gap` | 0.5 | Allowed gap between flashes |
| `--prefilter-luma` | 45 | Skip the scan when the last frame is brighter than this, 0 = off |
| `--head` | off | Also detect and trim leading black |
| `--head-threshold` | 32 | Leading strict threshold (stricter than the tail) |
| `--dark-luma` | 30 | Luma threshold for the curve check, 0 = off |
| `--encoder` | `auto` | `auto` / `libx264` / `h264_nvenc` / `hevc_nvenc` |
| `--crf` | 18 | x264 quality, lower = better |
| `--hw-cq` | 22 | NVENC quality, lower = sharper and bigger |
| `--copy` | off | Stream copy, fastest but the cut lands on a keyframe |
| `--workers` | 4 | Worker threads |
| `--dry-run` | off | List results only |
| `--skip-undetected` | off | Do not copy files where nothing was detected |
| `--lang` | `zh` | Output language: `zh` / `en` |
| `--report` | — | Write the results to a CSV |
| `--ffmpeg-dir` | — | Folder holding ffmpeg / ffprobe (default: bundled `bin\`, then `PATH`) |
| `--no-download` | off | Never auto-download ffmpeg; fail instead (scripts / CI) |
| `--install-ffmpeg` | off | Only download ffmpeg into `bin\` and exit |

Full list: `python trim_black_tail.py --help`.

## Report fields

```text
file / duration(s) / mode / type / head_removed(s) / field_start(s) / cut_at(s) / removed(s)
    / tail_luma / status / output / rescanned / flagged / note
```

`type` records whether a black or a white field was cut this time.

`note` explains the decision, e.g. “strict black start 24.800s, 1.35s continuous” or “tail prefilter: luma 126 > 45, scan skipped”, so you can review thresholds later.

## Tuning

| Symptom | Adjustment |
|---|---|
| Dark scenes mistaken for black | Lower `--core-threshold` (e.g. 56, 48) |
| A real field is missed | Raise `--core-threshold` (e.g. 72, 80) |
| Several flashes, only the last one cut | Raise `--flash-gap` |
| A hard cut (0.1–0.2 s) is missed | Lower `--core-min-black` |
| A fade is cut too late | Lower `--curve-min-drop` |
| White flash not detected | Add `--flash white` or `--flash both` |
| White scene falsely detected | Lower `--core-threshold` (e.g. 56, 48) — white needs to be purer |
| Scanning is slow | Lower `--scan-tail`, or raise `--prefilter-luma` (e.g. 50) |

## Stopping and resuming

- Create `STOP.txt` next to the script: it stops after the current file, keeping finished results
- Closing the console window triggers cleanup and terminates every ffmpeg child process
- To resume: `tools\只重跑失败.bat` on the same folder, or `--retry-failed report.csv`
  the report is merged on resume — earlier rows stay, only matching files are updated

## Notes

- The GPU only accelerates the final encode; scanning and luma checks stay on the CPU because ffmpeg's `blackframe` / `signalstats` filters are CPU-only
- With `--recursive` the relative subfolder structure is preserved, so identically named files never overwrite each other

## Follow the author

My Bilibili profile is where I post updates and other editing-related stuff first. Issues are welcome on the repository.

| Where | Link |
|---|---|
| **Bilibili profile (author)** | <https://space.bilibili.com/267072796> |
| Open the profile in the app (mobile) | `bilibili://space/267072796` |
| GitHub profile | <https://github.com/lihe233> |
| Repository | <https://github.com/lihe233/trim-black-tail> |
| Issues | <https://github.com/lihe233/trim-black-tail/issues> |

> Bilibili has no official “follow in one click” link — following requires a signed-in session and an explicit confirmation, and the platform deliberately does not expose such an endpoint, so any third-party “one-click follow” link is unreliable. The shortest path is the `bilibili://space/267072796` deeplink above: it opens the profile directly in the app, and you tap Follow once.

## License

MIT © lihe233
