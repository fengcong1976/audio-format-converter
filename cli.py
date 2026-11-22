"""命令行入口：批量转换音频格式（与图形界面共用同一套引擎）。

    python cli.py --to wav 样本.mp3
    python cli.py --to mp3 --bitrate 192k ./音乐
    python cli.py --to flac --start 30 --end 90 a.mp3 b.wav
    python cli.py --to mp3 --gain 150 --fade 2 ./素材
    python cli.py --version

打包后的用法：音频格式转换工具.exe --cli --to mp3 --bitrate 192k 文件...
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

from core.about import APP_NAME, DISCLOSURE, VERSION_LINE
from core.audio_job import (BITRATE_CHOICES, DEFAULT_BITRATE, DEFAULT_TARGET,
                            LOSSLESS, OUT_DIRNAME, TARGETS, convert_batch,
                            write_reports)
from core.compliance import NetworkAudit
from core.ffmpeg_tool import FFmpegMissing, version_text


def _mb(n: int) -> str:
    if not n:
        return "—"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=APP_NAME,
        description="把音频（或视频里的音轨）转成常见格式，本地处理、不覆盖原文件。")
    p.add_argument("paths", nargs="*", help="音频 / 视频文件或文件夹（可多个）")
    p.add_argument("--to", dest="dst_format", choices=TARGETS, default=DEFAULT_TARGET,
                   help=f"输出格式（默认 {DEFAULT_TARGET}）：mp3 / m4a / ogg 有损，flac / wav 无损")
    p.add_argument("--bitrate", choices=BITRATE_CHOICES, default=DEFAULT_BITRATE,
                   help="码率（仅 mp3 / m4a / ogg 生效；keep = 保持原码率）")
    p.add_argument("--target-mb", type=float, default=0.0, metavar="MB",
                   help="限制单个文件的目标大小（MB），码率按它自动估算；"
                        "与 --bitrate 二选一")
    p.add_argument("--start", type=float, default=0.0, metavar="秒",
                   help="剪切起点（秒，默认 0）")
    p.add_argument("--end", type=float, default=0.0, metavar="秒",
                   help="剪切终点（秒，默认 0 = 到最后）")
    p.add_argument("--gain", type=int, default=100, metavar="%",
                   help="音量增益 0–200（默认 100 = 不改动）")
    p.add_argument("--fade", type=float, default=0.0, metavar="秒",
                   help="淡入淡出秒数（默认 0 = 关）")
    p.add_argument("-o", "--out", default=None,
                   help=f"统一输出目录（默认每个文件旁的 {OUT_DIRNAME}）")
    p.add_argument("--open", action="store_true", help="转完打开结果文件夹")
    p.add_argument("--version", action="store_true", help="显示版本信息")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if args.version:
        print(VERSION_LINE)
        print("更多免费软件：www.mrheedu.com")
        print(DISCLOSURE)
        try:
            print(f"ffmpeg：{version_text()}")
        except FFmpegMissing as exc:
            print(f"ffmpeg：{exc}")
        return 0
    if not args.paths:
        build_parser().print_help()
        return 2
    if not 0 <= args.gain <= 200:
        print("音量增益要在 0–200 之间（100 = 不改动）", file=sys.stderr)
        return 2
    if args.start < 0 or args.end < 0:
        print("剪切时间不能是负数", file=sys.stderr)
        return 2
    if args.end and args.end <= args.start:
        print("--end 要大于 --start（想转到最后就别填 --end）", file=sys.stderr)
        return 2
    if args.fade < 0:
        print("--fade 不能是负数", file=sys.stderr)
        return 2
    if args.target_mb < 0:
        print("--target-mb 不能是负数", file=sys.stderr)
        return 2
    if args.target_mb > 0 and args.bitrate != DEFAULT_BITRATE:
        print("--target-mb 与 --bitrate 只能选一个：指定目标大小后码率会自动算",
              file=sys.stderr)
        return 2

    audit = NetworkAudit().__enter__()
    try:
        def on_item(item):
            if item.skipped:
                flag = "跳过"
            elif item.ok:
                flag = "直通" if item.passthrough else "完成"
            else:
                flag = "失败"
            note = (item.note or "").split("；")[0][:60]
            print(f"[{flag}] {item.src}")
            if item.ok:
                print(f"       {_mb(item.src_bytes)} → {_mb(item.dst_bytes)} ｜ "
                      f"{item.src_format} → {item.dst_format} ｜ "
                      f"码率 {item.src_bitrate:.0f} → {item.dst_bitrate:.0f} kbps ｜ "
                      f"{item.elapsed * 1000:.0f} ms")
            if note:
                print(f"       {note}")

        report = convert_batch(
            args.paths, dst_format=args.dst_format, bitrate=args.bitrate,
            start=args.start, end=args.end, gain=args.gain, fade=args.fade,
            out_dir=args.out, on_item=on_item, progress_cb=lambda f: None,
            target_mb=args.target_mb)
    except FFmpegMissing as exc:
        print(f"\n{exc}", file=sys.stderr)
        return 3
    except KeyboardInterrupt:
        print("\n已中断：已经转好的结果保留在原文件旁的 out 里。")
        return 130
    finally:
        audit.__exit__(None, None, None)

    print(f"\n{audit.summary()}")
    extra = ""
    if args.dst_format in LOSSLESS:
        extra = " ｜ 无损格式，码率设置不生效"
    elif args.target_mb > 0:
        extra = f" ｜ 目标大小 {args.target_mb:g} MB"
    print(f"\n目标格式 {args.dst_format}{extra} ｜ 共 {report.total} 个文件 ｜ "
          f"完成 {len(report.succeeded)} ｜ 失败 {len(report.failed)} ｜ "
          f"跳过 {len(report.skipped)} ｜ 直通（未重编码）{len(report.passthrough)}")
    print("-" * 78)
    for item in report.items:
        # 这里刻意用 ASCII 标记：控制台重定向到文件时是 GBK 编码，
        # emoji 会直接 UnicodeEncodeError 把整个命令打崩。
        mark = "[完成]" if item.ok else ("[跳过]" if item.skipped else "[失败]")
        print(f"{mark} {item.src}")
        if item.ok:
            print(f"       {item.src_format} → {item.dst_format} ｜ "
                  f"{item.src_bitrate:.0f} → {item.dst_bitrate:.0f} kbps ｜ "
                  f"{item.out_duration:.1f}s ｜ 原文件未改动："
                  f"{'是' if item.src_unchanged else '未核实'}")
        if item.note:
            print(f"       {item.note}")
    print("-" * 78)

    for p in write_reports(report):
        print(f"报告：{p}")

    dirs = report.out_dirs or ([args.out] if args.out else [])
    if args.open and dirs:
        target = dirs[0]
        try:
            os.startfile(target)  # noqa: S606 - 本地打开文件夹，不联网
        except (AttributeError, OSError):
            subprocess.Popen(["explorer", target])
        print(f"结果位置：{target}")
    return 0 if not report.failed else 1


if __name__ == "__main__":
    sys.exit(main())
