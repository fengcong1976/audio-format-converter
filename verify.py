"""验收脚本：30 项断言，全部用引擎直连（不依赖 PyQt），跑完给一句结论。

    python verify.py            # 全量验收（会现场合成素材，约十几秒）
    python verify.py --keep     # 保留临时素材目录，便于人工复听

为什么这些检查要写得这么细：音频工具最容易「看起来成功了」——
有损转无损、直通被悄悄重编码、原文件被改写、剪切越界静默截断，
这几种情况都不会报错。所以每一条都要拿产物实测（时长、码率、音量、
解码后 PCM 的 md5），不靠返回值自证。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from core.audio_job import (  # noqa: E402
    _keep_kbps, collect_audio, convert_batch, convert_one, measured_kbps,
    write_reports,
)
from core.compliance import NetworkAudit  # noqa: E402
from core.ffmpeg_tool import (  # noqa: E402
    find_tools, probe_audio, run_ffmpeg,
)

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    flag = "PASS" if cond else "FAIL"
    line = f"  {flag}  {name}"
    if detail:
        line += f"（{detail}）"
    print(line, flush=True)
    return bool(cond)


def section(title: str) -> None:
    print(f"\n{title}", flush=True)


# ---------------------------------------------------------------- 素材与测量

def ff(args: list[str]) -> tuple[int, str]:
    """直接调随包 ffmpeg（验收脚本自己生成素材，不经过被测代码）。"""
    ffmpeg, _ = find_tools()
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-y"] + args,
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return proc.returncode, (proc.stderr or "").strip()


def make_sine(path: Path, seconds: float, codec_args: list[str],
              amplitude: float = 1.0) -> bool:
    """合成正弦波素材；amplitude<1 是为了留给音量提升的余量，避免削波。"""
    chain = ["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}"]
    if amplitude < 1.0:
        chain += ["-af", f"volume={amplitude}"]
    rc, err = ff(chain + codec_args + [str(path)])
    if rc != 0:
        print(f"    （素材合成失败：{err[-200:]}）", flush=True)
    return rc == 0


def probe(path: Path) -> dict:
    _, ffprobe = find_tools()
    proc = subprocess.run(
        [str(ffprobe), "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    try:
        return json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {}


def info_of(path: Path):
    return probe_audio(path)


def pcm_md5(path: Path) -> str:
    """解码成 PCM 再算 md5：直通是否真的没动过音频，这一项最硬。"""
    ffmpeg, _ = find_tools()
    proc = subprocess.run(
        [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-i", str(path),
         "-map", "0:a:0", "-c:a", "pcm_s16le", "-f", "md5", "-"],
        capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (proc.stdout or "").strip()


def mean_db(path: Path, start: float | None = None,
            dur: float | None = None) -> float:
    """volumedetect 的 mean_volume：用来量音量提升与淡入淡出是否真的生效。"""
    ffmpeg, _ = find_tools()
    args = [str(ffmpeg), "-hide_banner", "-nostats"]
    if start is not None:
        args += ["-ss", f"{start:.3f}"]
    if dur is not None:
        args += ["-t", f"{dur:.3f}"]
    args += ["-i", str(path), "-map", "0:a:0", "-af", "volumedetect", "-f", "null", "-"]
    proc = subprocess.run(args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf)\s*dB", proc.stderr or "")
    if not m:
        return -200.0
    return -200.0 if m.group(1) == "-inf" else float(m.group(1))


def sha(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def audio_stream(path: Path) -> dict:
    for s in probe(path).get("streams") or []:
        if s.get("codec_type") == "audio":
            return s
    return {}


def duration_of(path: Path) -> float:
    try:
        return float((probe(path).get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        return 0.0


# ---------------------------------------------------------------- 主流程

def main() -> int:
    keep = "--keep" in sys.argv
    base = Path(tempfile.mkdtemp(prefix="ac-verify-"))
    print(f"素材目录：{base}", flush=True)
    try:
        return run_all(base)
    finally:
        if keep:
            print(f"\n已保留素材目录：{base}")
        else:
            shutil.rmtree(base, ignore_errors=True)


def run_all(base: Path) -> int:
    # ---------- 素材
    mp3 = base / "sample_10s.mp3"
    flac = base / "sample_10s.flac"
    wav = base / "sample_10s.wav"
    aac = base / "sample_10s.aac"
    mp3_128 = base / "sample_128.mp3"
    quiet = base / "quiet_10s.wav"     # 25% 振幅，留给音量提升的余量
    short = base / "short_3s.mp3"
    ogg_vbr = base / "sample_vbr.ogg"   # Vorbis 标称码率虚高，专门验证「保持原码率」的修正
    video = base / "movie_with_audio.mp4"
    video_no_audio = base / "movie_silent.mp4"
    broken = base / "broken.mp3"

    ok_samples = all([
        make_sine(mp3, 10, ["-c:a", "libmp3lame", "-b:a", "192k"]),
        make_sine(flac, 10, ["-c:a", "flac"]),
        make_sine(wav, 10, ["-c:a", "pcm_s16le"]),
        make_sine(aac, 10, ["-c:a", "aac", "-b:a", "128k", "-f", "adts"]),
        make_sine(mp3_128, 10, ["-c:a", "libmp3lame", "-b:a", "128k"]),
        make_sine(quiet, 10, ["-c:a", "pcm_s16le"], amplitude=0.25),
        make_sine(short, 3, ["-c:a", "libmp3lame", "-b:a", "192k"]),
        make_sine(ogg_vbr, 10, ["-c:a", "libvorbis", "-q:a", "2"]),
    ])
    rc_v, _ = ff(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=6",
                  "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
                  "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(video)])
    rc_n, _ = ff(["-f", "lavfi", "-i", "testsrc2=size=320x240:rate=15:duration=6",
                  "-c:v", "mpeg4", str(video_no_audio)])
    broken.write_bytes(b"this is not an audio file" * 50)
    ok_samples = ok_samples and rc_v == 0 and rc_n == 0
    src_hashes = {p.name: sha(p) for p in
                  (mp3, flac, wav, aac, mp3_128, quiet, short, video)}
    print(f"素材就绪：{'是' if ok_samples else '否（后面相关项会失败）'}", flush=True)
    if not ok_samples:
        print("素材合成失败，无法继续验收", flush=True)
        return 1

    out = base / "out"

    # ---------- 01 mp3 → wav：时长对得上，编码变 PCM
    section("一、基本转换")
    r = convert_one(mp3, "wav", "keep", out_dir=out)
    d = duration_of(Path(r.dst)) if r.ok else 0
    check("01 mp3 → wav 成功，时长误差 ≤ 0.1s，编码为 pcm_s16le",
          r.ok and abs(d - 10) <= 0.1 and audio_stream(Path(r.dst)).get("codec_name") == "pcm_s16le",
          f"{r.note} / {d:.2f}s / {audio_stream(Path(r.dst)).get('codec_name')}")

    # ---------- 02 wav → flac
    r = convert_one(wav, "flac", "keep", out_dir=out / "02")
    check("02 wav → flac 输出编码为 flac，时长一致",
          r.ok and audio_stream(Path(r.dst)).get("codec_name") == "flac"
          and abs(duration_of(Path(r.dst)) - 10) <= 0.1,
          f"{r.note}")

    # ---------- 03 flac → mp3 192k
    r = convert_one(flac, "mp3", "192k", out_dir=out / "03")
    st = audio_stream(Path(r.dst)) if r.ok else {}
    kbps = float(st.get("bit_rate") or 0) / 1000
    check("03 flac → mp3 192k：编码 mp3，实测码率 180–205kbps",
          r.ok and st.get("codec_name") == "mp3" and 180 <= kbps <= 205,
          f"{kbps:.0f} kbps")

    # ---------- 04 mp3 → m4a
    r = convert_one(mp3, "m4a", "keep", out_dir=out / "04")
    check("04 mp3 → m4a 输出编码为 aac",
          r.ok and audio_stream(Path(r.dst)).get("codec_name") == "aac",
          f"{r.note}")

    # ---------- 05 flac → ogg
    r = convert_one(flac, "ogg", "192k", out_dir=out / "05")
    check("05 flac → ogg 输出编码为 vorbis",
          r.ok and audio_stream(Path(r.dst)).get("codec_name") == "vorbis",
          f"{r.note}")

    section("二、直通与重编码的判断")
    # ---------- 06 同码率 mp3 → mp3 直通
    r6 = convert_one(mp3, "mp3", "keep", out_dir=out / "06")
    same = r6.ok and pcm_md5(Path(r6.dst)) == pcm_md5(mp3)
    check("06 mp3 192k → mp3 保持原码率：走直通，解码后 PCM md5 相同",
          same and r6.passthrough and not r6.reencoded,
          f"直通={r6.passthrough} / {r6.note}")

    # ---------- 07 128k → 320k 必须重编码
    r7 = convert_one(mp3_128, "mp3", "320k", out_dir=out / "07")
    st7 = audio_stream(Path(r7.dst)) if r7.ok else {}
    k7 = float(st7.get("bit_rate") or 0) / 1000
    check("07 mp3 128k → mp3 320k：不走直通，实测码率 300–340kbps",
          r7.ok and not r7.passthrough and r7.reencoded and 300 <= k7 <= 340,
          f"{k7:.0f} kbps / 直通={r7.passthrough}")

    # ---------- 08 aac → m4a 只换封装，直通
    r8 = convert_one(aac, "m4a", "keep", out_dir=out / "08")
    same8 = r8.ok and pcm_md5(Path(r8.dst)) == pcm_md5(aac)
    check("08 aac → m4a 只换封装：走直通，解码后 PCM md5 相同",
          same8 and r8.passthrough,
          f"直通={r8.passthrough} / {r8.note}")

    section("三、剪切、音量、淡入淡出")
    # ---------- 09 剪切 2–5 秒
    r9 = convert_one(mp3, "wav", "keep", start=2, end=5, out_dir=out / "09")
    d9 = duration_of(Path(r9.dst)) if r9.ok else 0
    check("09 剪切 2–5 秒：产物时长 3.0±0.1s",
          r9.ok and abs(d9 - 3) <= 0.1, f"{d9:.2f}s / {r9.note}")

    # ---------- 10 只填起点 8 秒
    r10 = convert_one(mp3, "wav", "keep", start=8, out_dir=out / "10")
    d10 = duration_of(Path(r10.dst)) if r10.ok else 0
    check("10 只填起点 8 秒：产物时长 2.0±0.1s",
          r10.ok and abs(d10 - 2) <= 0.1, f"{d10:.2f}s")

    # ---------- 11 起点 ≥ 终点
    r11 = convert_one(mp3, "wav", "keep", start=5, end=3, out_dir=out / "11")
    check("11 起点 5 秒 ≥ 终点 3 秒：拒绝且说明原因，不产出文件",
          (not r11.ok) and "起始时间要小于结束时间" in r11.note and not r11.dst,
          r11.note)

    # ---------- 12 起点超出总时长
    r12 = convert_one(mp3, "wav", "keep", start=99, out_dir=out / "12")
    check("12 起点 99 秒 > 总时长：拒绝并提示总时长，不产出文件",
          (not r12.ok) and "总时长" in r12.note and not r12.dst, r12.note)

    # ---------- 13 音量 200%
    r13 = convert_one(quiet, "wav", "keep", gain=200, out_dir=out / "13")
    db_src, db_dst = mean_db(quiet), mean_db(Path(r13.dst)) if r13.ok else -200
    delta = db_dst - db_src
    check("13 音量 200%：实测提升约 +6dB（容差 1.5dB）",
          r13.ok and abs(delta - 6.02) <= 1.5,
          f"{db_src:.1f} → {db_dst:.1f} dB（{delta:+.1f}）")

    # ---------- 14 音量 0% 静音
    r14 = convert_one(quiet, "wav", "keep", gain=0, out_dir=out / "14")
    db14 = mean_db(Path(r14.dst)) if r14.ok else 0
    check("14 音量 0%：产物是静音（mean_volume < -70dB）",
          r14.ok and db14 < -70, f"{db14:.1f} dB")

    # ---------- 15 淡入：开头明显小于 1.8 秒处
    r15 = convert_one(quiet, "wav", "keep", fade=2, out_dir=out / "15")
    head = mean_db(Path(r15.dst), 0, 0.1) if r15.ok else 0
    middle = mean_db(Path(r15.dst), 1.6, 0.2) if r15.ok else 0
    check("15 淡入 2s：开头 0.1s 比 1.6s 处低 20dB 以上",
          r15.ok and head < middle - 20, f"{head:.1f} vs {middle:.1f} dB")

    # ---------- 16 淡出：结尾明显小于中段
    r16 = convert_one(quiet, "wav", "keep", fade=2, out_dir=out / "16")
    tail = mean_db(Path(r16.dst), 9.9, 0.1) if r16.ok else 0
    middle16 = mean_db(Path(r16.dst), 4.0, 0.5) if r16.ok else 0
    check("16 淡出 2s：最后 0.1s 比中段低 20dB 以上",
          r16.ok and tail < middle16 - 20, f"{tail:.1f} vs {middle16:.1f} dB")

    # ---------- 17 3 秒音频填 5 秒淡入淡出
    r17 = convert_one(short, "wav", "keep", fade=5, out_dir=out / "17")
    d17 = duration_of(Path(r17.dst)) if r17.ok else 0
    check("17 3 秒音频填 5 秒淡入淡出：自动收成约一半并说明，不报错",
          r17.ok and r17.fade_used < 2.6
          and abs(r17.fade_used - d17 / 2) <= 0.05
          and "已收到" in r17.note and abs(d17 - 3) <= 0.1,
          f"fade_used={r17.fade_used:g} / 产物 {d17:.2f}s / {r17.note}")

    section("四、视频音轨、取消、命名与目录")
    # ---------- 18 带音频的视频
    r18 = convert_one(video, "mp3", "keep", out_dir=out / "18")
    d18 = duration_of(Path(r18.dst)) if r18.ok else 0
    check("18 视频文件取第 1 条音轨：成功、from_video 为真、时长 6s",
          r18.ok and r18.from_video and 5.8 <= d18 <= 6.2,
          f"{d18:.2f}s / {r18.note}")

    # ---------- 19 无声视频
    r19 = convert_one(video_no_audio, "mp3", "keep", out_dir=out / "19")
    check("19 没有音轨的视频：跳过并说明「没有音频轨道」，不产出文件",
          r19.skipped and "没有音频轨道" in r19.note and not r19.dst, r19.note)

    # ---------- 20 中途取消
    long_wav = base / "long_900s.wav"
    made_long = make_sine(long_wav, 900, ["-c:a", "pcm_s16le"])
    if not made_long:
        check("20 取消转换：不留下半截文件，原文件不变", False, "长素材合成失败")
    else:
        long_hash = sha(long_wav)
        cancel_out = base / "cancel_out"
        cancel_out.mkdir(exist_ok=True)
        ev = threading.Event()
        timer = threading.Timer(0.25, ev.set)
        timer.start()
        rep20 = convert_batch([long_wav], "flac", "keep",
                              out_dir=cancel_out, cancel=ev)
        timer.cancel()
        leftover = list(cancel_out.glob("*.flac"))
        check("20 中途取消：无半截文件残留，原文件哈希不变",
              rep20.cancelled and not leftover and sha(long_wav) == long_hash,
              f"cancelled={rep20.cancelled} / 残留 {len(leftover)} 个 / "
              f"{rep20.items[0].note if rep20.items else '无记录'}")

    # ---------- 21 同名不覆盖
    name_out = out / "21"
    a1 = convert_one(mp3, "flac", "keep", out_dir=name_out)
    a2 = convert_one(mp3, "flac", "keep", out_dir=name_out)
    names = sorted(p.name for p in name_out.glob("*.flac"))
    check("21 连转两次同名文件：自动加序号，两个结果都在，原产物不被覆盖",
          a1.ok and a2.ok and len(names) == 2 and a1.dst != a2.dst,
          "、".join(names))

    # ---------- 22 原文件字节不变
    changed = [n for n, h in src_hashes.items() if sha(base / n) != h]
    check("22 全部素材在处理后 SHA-256 不变（原文件只读）",
          not changed, f"被改动的：{changed}" if changed else "8 个素材一致")

    # ---------- 23 结果落在 out 且 report.json 可解析
    case = base / "case23"
    case.mkdir()
    shutil.copy2(mp3, case / "a.mp3")
    shutil.copy2(wav, case / "b.wav")
    rep23 = convert_batch([case], "ogg", "128k")
    written = write_reports(rep23)
    report_file = case / "out" / "report.json"
    parsed = {}
    if report_file.is_file():
        try:
            parsed = json.loads(report_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            parsed = {}
    check("23 结果写在源文件旁的 out 目录，report.json 可解析且条目齐全",
          report_file.is_file() and parsed.get("total") == 2
          and len(parsed.get("items") or []) == 2 and bool(written),
          f"{report_file}")

    # ---------- 24 递归收集跳过 out
    scan = base / "case24"
    (scan / "sub").mkdir(parents=True)
    (scan / "out").mkdir()
    shutil.copy2(mp3, scan / "top.mp3")
    shutil.copy2(mp3, scan / "sub" / "nested.mp3")
    shutil.copy2(mp3, scan / "out" / "already_done.mp3")
    found = collect_audio([scan])
    names24 = sorted(p.name for p in found)
    check("24 递归扫描子文件夹，但跳过 out（不把结果再转一遍）",
          names24 == ["nested.mp3", "top.mp3"], "、".join(names24))

    # ---------- 25 中文与空格
    tricky = base / "中文 名称 测试.mp3"
    shutil.copy2(mp3, tricky)
    r25 = convert_one(tricky, "m4a", "128k", out_dir=base / "out25")
    check("25 中文与空格的路径能正常转换，产物可解析",
          r25.ok and Path(r25.dst).is_file()
          and duration_of(Path(r25.dst)) > 9.5,
          Path(r25.dst).name if r25.dst else r25.note)

    # ---------- 26 损坏文件
    try:
        rep26 = convert_batch([broken], "mp3", "keep", out_dir=base / "out26")
        item26 = rep26.items[0]
        raised = False
    except Exception as exc:  # 不许把异常抛给界面
        raised, item26 = True, None
        print(f"    异常：{exc}", flush=True)
    check("26 损坏文件：记成跳过并说明原因，批量不抛异常",
          not raised and item26 is not None and item26.skipped and item26.note,
          (item26.note if item26 else "抛异常了"))

    section("五、披露、进度与自检")
    # ---------- 27 flac 目标忽略码率且说明
    r27 = convert_one(mp3, "flac", "128k", out_dir=out / "27")
    check("27 flac 目标选 128k：无损格式忽略码率，并在说明里写清楚",
          r27.ok and "码率设置已忽略" in r27.note,
          r27.note)

    # ---------- 28 进度单调不减
    prog: list[float] = []
    rep28 = convert_batch([mp3, wav, flac], "m4a", "128k",
                          out_dir=base / "out28", progress_cb=prog.append)
    monotonic = all(b >= a - 1e-9 for a, b in zip(prog, prog[1:]))
    check("28 批量进度单调不减，结束时到 100%",
          bool(prog) and monotonic and prog[-1] >= 0.999 and len(rep28.items) == 3,
          f"{len(prog)} 次回调 / 末值 {prog[-1] if prog else 0:.2f}")

    # ---------- 29 全程 0 联网
    with NetworkAudit() as audit:
        convert_batch([mp3, flac], "wav", "keep", out_dir=base / "out29")
    check("29 转换全程 0 次联网（NetworkAudit 实测）",
          audit.attempts == 0, audit.summary())

    # ---------- 30 打包自检入口可用
    log = base / "selftest.txt"
    proc = subprocess.run(
        [sys.executable, str(ROOT / "main.py"), "--selftest", str(log)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=str(ROOT))
    text = log.read_text(encoding="utf-8") if log.is_file() else ""
    check("30 无界面自检入口（--selftest）跑通，日志写入指定路径",
          proc.returncode == 0 and "结论：全部通过" in text,
          text.strip().splitlines()[-1] if text.strip() else (proc.stderr or "")[-120:])

    # ---------- 31 「保持原码率」对虚高标称值的修正
    k_high, note_high = _keep_kbps(500, int(863 * 1024), 65, "vorbis", "mp3")
    k_ok, note_ok = _keep_kbps(320, int(2.4 * 1048576), 69, "mp3", "mp3")
    check("31 标称码率虚高时按体积反推：500k/108k → 96k（封顶 192k），正常文件照抄",
          abs(k_high - 96) <= 1 and "偏高" in note_high
          and abs(k_ok - 320) < 1 and not note_ok,
          f"{k_high:.0f}k（{note_high}）/ {k_ok:.0f}k")

    # ---------- 32 真实 ogg（Vorbis）保持原码率转 mp3
    r32 = convert_one(ogg_vbr, "mp3", "keep", out_dir=out / "32")
    st32 = audio_stream(Path(r32.dst)) if r32.ok else {}
    kbps32 = float(st32.get("bit_rate") or 0) / 1000
    avg32 = measured_kbps(ogg_vbr.stat().st_size, 10)
    check("32 ogg(VBR) → mp3 保持原码率：不照抄标称值，说明里写清实测平均",
          r32.ok and "偏高" in r32.note and avg32 < probe_audio(ogg_vbr).bitrate
          and kbps32 <= 200,
          f"标称 {probe_audio(ogg_vbr).bitrate:.0f}k / 实测约 {avg32:.0f}k / "
          f"产物 {kbps32:.0f}k / {r32.note}")

    # ---------- 33 限制目标大小：按体积反推码率，转完不超目标
    limit_bytes = int(0.2 * 1048576)
    r33 = convert_one(wav, "mp3", "keep", out_dir=out / "33", target_mb=0.2)
    check("33 限制目标大小 0.2MB：产物 ≤ 目标、时长不变、说明里写清目标",
          r33.ok and r33.target_bytes == limit_bytes
          and r33.dst_bytes <= limit_bytes
          and abs(duration_of(Path(r33.dst)) - 10) <= 0.1
          and "目标" in r33.note,
          f"{r33.dst_bytes} / {limit_bytes} 字节 · {r33.note}")

    # ---------- 汇总
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n" + "=" * 72, flush=True)
    if passed == total:
        print(f"验收通过：{passed}/{total} 项全部通过", flush=True)
        return 0
    print(f"验收未通过：{passed}/{total} 项通过，失败 {total - passed} 项", flush=True)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  FAIL  {name}  {detail}", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
