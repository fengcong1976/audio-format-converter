"""音频转换核心：探测 → 判断能否直通 → 拼 ffmpeg 参数 → 逐条报告。

三条原则：
  · 原文件只读不写，结果全部落在源文件旁边的 out 目录，同名自动加序号；
  · 能直通就直通（源编码 == 目标编码且没有任何处理），直通不重编码，音质与原文件一致；
  · 只要重新编码，逐条写清「有没有重新编码、码率从多少变到多少」，不写「无损转换」。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .ffmpeg_tool import (FFmpegMissing, describe_failure, probe_audio,
                          run_ffmpeg)

AUDIO_EXTS = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".wma",
              ".ape", ".opus", ".aiff"}
VIDEO_EXTS = {".mp4", ".mkv", ".mov", ".avi", ".flv", ".webm", ".ts",
              ".wmv", ".m4v"}
ALL_EXTS = AUDIO_EXTS | VIDEO_EXTS
OUT_DIRNAME = "out"
DEFAULT_TARGET = "mp3"
DEFAULT_BITRATE = "keep"     # keep / 320k / 192k / 128k / 64k
BITRATE_CHOICES = ("keep", "320k", "192k", "128k", "64k")
BITRATE_FALLBACK = 192.0     # 「保持原码率」但源文件没写码率时的回落值
KBPS_MAX = 320.0             # 再高只增体积不增听感，mp3 到头就是 320k
KBPS_MIN = 32.0              # 按目标大小算出的码率下限，再低就没法听了
SIZE_SAFETY = 0.96           # 目标体积只用 96% 当预算，余量留给封装开销
VBR_SLACK = 1.25             # 标称码率超过体积实测的 1.25 倍，就当作虚高
VBR_KEEP_CAP = 192.0         # 标称虚高时最多按 192k 输出
MP3_LADDER = (32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320)

# 目标格式 → (编码器, 扩展名)
ENCODERS = {
    "mp3": ("libmp3lame", ".mp3"),
    "m4a": ("aac", ".m4a"),
    "ogg": ("libvorbis", ".ogg"),
    "flac": ("flac", ".flac"),
    "wav": ("pcm_s16le", ".wav"),
}
LOSSLESS = {"flac", "wav"}
TARGETS = tuple(ENCODERS)    # 界面下拉的顺序：mp3 / m4a / ogg / flac / wav


def _clock(seconds: float) -> str:
    """时长写成 03:12，一眼知道多长。"""
    if not seconds:
        return "—"
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


def _mb(n: int) -> str:
    if not n:
        return "—"
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass
class ItemReport:
    src: str
    src_format: str = ""          # mp3 / wav / mp4(视频)
    dst_format: str = ""          # mp3 / m4a / ogg / flac / wav
    src_codec: str = ""
    dst_codec: str = ""
    dst_encoder: str = ""
    src_bitrate: float = 0.0
    dst_bitrate: float = 0.0
    src_sample_rate: int = 0
    dst_sample_rate: int = 0
    src_channels: int = 0
    dst_channels: int = 0
    duration: float = 0.0
    out_duration: float = 0.0
    from_video: bool = False
    start: float = 0.0            # 剪切起点
    end: float = 0.0              # 0 = 到最后
    gain_percent: int = 100
    fade: float = 0.0             # 用户填的淡入淡出秒数
    fade_used: float = 0.0        # 实际生效的秒数（太短会被收小）
    target_kbps: float = 0.0
    target_bytes: int = 0         # 限制目标大小时的体积上限（0 = 不限）
    reencoded: bool = True
    passthrough: bool = False     # 直通（未重新编码）
    src_bytes: int = 0
    dst: str = ""
    dst_bytes: int = 0
    ok: bool = False
    skipped: bool = False
    note: str = ""
    elapsed: float = 0.0
    src_sha256_before: str = ""
    src_unchanged: bool | None = None

    def _quality_text(self) -> str:
        if self.skipped:
            return self.note or "—"
        if not self.ok:
            return self.note or "未交付结果"
        if self.passthrough:
            return "未重新编码，音质与原文件一致"
        if self.dst_format in LOSSLESS:
            return "无损格式，未做有损压缩"
        return "有损重编码：高频细节会减少，码率越低越明显"

    def disclosure(self) -> list[tuple[str, str]]:
        """逐条披露：界面详情页与 report.json 共用这一份。"""
        def rate(v: float) -> str:
            return f"{v:.0f} kbps" if v else "未标注"

        if self.skipped:
            head = ("是否重新编码", "否（跳过，未处理）")
        elif not self.ok:
            head = ("是否重新编码", "否（未交付结果）")
        elif self.passthrough:
            head = ("是否重新编码", "否（原编码直通复制）")
        else:
            enc = self.dst_encoder or ENCODERS.get(self.dst_format, ("",))[0]
            kbps = f" {self.dst_bitrate:.0f}k" if self.dst_bitrate else ""
            head = ("是否重新编码",
                    f"是（{self.dst_format.upper()} / {enc}{kbps}）")

        rows: list[tuple[str, str]] = [head]
        rows.append(("格式", f"{self.src_format or '—'} → {self.dst_format or '—'}"))
        rows.append(("码率", (f"{rate(self.src_bitrate)} → {rate(self.dst_bitrate)}"
                              if self.ok and self.dst_bitrate
                              else rate(self.src_bitrate))))
        rows.append(("采样率", (f"{self.src_sample_rate} Hz → {self.dst_sample_rate} Hz"
                                if self.ok and self.dst_sample_rate
                                else f"{self.src_sample_rate} Hz")))
        rows.append(("声道", (f"{self.src_channels} → {self.dst_channels}"
                              if self.ok and self.dst_channels
                              else f"{self.src_channels}")))
        if self.start > 0 or self.end > 0:
            rows.append(("时长", f"{_clock(self.duration)} → {_clock(self.out_duration)}"))
        else:
            rows.append(("时长", _clock(self.duration)))
        if self.gain_percent != 100:
            rows.append(("音量", f"100% → {self.gain_percent}%"))
        rows.append(("淡入淡出", (f"淡入 {self.fade_used:g}s / 淡出 {self.fade_used:g}s"
                                if self.fade_used > 0 else "关")))
        if self.from_video:
            rows.append(("来源", "视频文件，取第 1 条音轨"))
        rows.append(("文件大小", f"{_mb(self.src_bytes)} → {_mb(self.dst_bytes)}"
                                 if self.dst_bytes else _mb(self.src_bytes)))
        if self.target_bytes and self.ok:
            gap = "，未达标" if self.dst_bytes > self.target_bytes else ""
            rows.append(("目标大小", f"≤ {_mb(self.target_bytes)}"
                                     f"（实测 {_mb(self.dst_bytes)}{gap}）"))
        rows.append(("有损说明", self._quality_text()))
        if self.src_unchanged is True:
            rows.append(("原文件是否改动", "未改动（SHA-256 前后一致）"))
        elif self.src_unchanged is False:
            rows.append(("原文件是否改动", "有变化！请核对源文件是否被别的程序改写"))
        else:
            rows.append(("原文件是否改动", "无法核实（读取源文件失败）"))
        rows.append(("耗时", f"{self.elapsed:.1f} s"))
        return rows


@dataclass
class BatchReport:
    dst_format: str = DEFAULT_TARGET
    bitrate: str = DEFAULT_BITRATE
    target_mb: float = 0.0        # > 0 = 按目标体积反推码率
    start: float = 0.0
    end: float = 0.0
    gain: int = 100
    fade: float = 0.0
    items: list = field(default_factory=list)
    elapsed: float = 0.0
    cancelled: bool = False
    out_dirs: list = field(default_factory=list)
    network_attempts: int | None = None

    @property
    def total(self) -> int:
        return len(self.items)

    @property
    def succeeded(self) -> list:
        return [i for i in self.items if i.ok]

    @property
    def failed(self) -> list:
        return [i for i in self.items if not i.ok and not i.skipped]

    @property
    def skipped(self) -> list:
        return [i for i in self.items if i.skipped]

    @property
    def passthrough(self) -> list:
        return [i for i in self.items if i.ok and i.passthrough]

    def to_dict(self) -> dict:
        return {
            "dst_format": self.dst_format,
            "bitrate": self.bitrate,
            "target_mb": self.target_mb,
            "start": self.start,
            "end": self.end,
            "gain": self.gain,
            "fade": self.fade,
            "elapsed": round(self.elapsed, 2),
            "cancelled": self.cancelled,
            "total": self.total,
            "succeeded": len(self.succeeded),
            "failed": len(self.failed),
            "skipped": len(self.skipped),
            "passthrough": len(self.passthrough),
            "out_dirs": list(self.out_dirs),
            "items": [asdict(i) for i in self.items],
        }


# ---------------------------------------------------------------- 文件与命名

def collect_audio(paths) -> list[Path]:
    """收集要处理的音频 / 视频；递归时跳过已生成的 out，免得把结果再转一遍。"""
    found: list[Path] = []
    seen: set[str] = set()
    for raw in paths:
        p = Path(raw)
        batch: list[Path] = []
        if p.is_dir():
            for f in sorted(p.rglob("*")):
                if not f.is_file() or f.suffix.lower() not in ALL_EXTS:
                    continue
                try:
                    rel = f.relative_to(p)
                except ValueError:  # pragma: no cover - rglob 的结果一定在 p 下
                    continue
                # 只跳过「扫描根之下的 out 目录」。不能写成 `OUT_DIRNAME in f.parts`：
                # 那样凡是路径上有一层叫 out 的父目录（例如选了 D:\out\music），
                # 整批文件都会被当成产物跳过。
                if OUT_DIRNAME in rel.parts:
                    continue
                batch.append(f)
        elif p.is_file() and p.suffix.lower() in ALL_EXTS:
            batch.append(p)
        for f in batch:
            key = str(f.resolve()).lower()
            if key not in seen:
                seen.add(key)
                found.append(f)
    return found


def _reserve_path(out_dir: Path, filename: str) -> Path:
    """原子占名：抢不到就换 `名字 (1).mp3`，绝不覆盖已有文件。"""
    stem, suffix = Path(filename).stem, Path(filename).suffix
    i = 0
    while True:
        p = out_dir / (filename if i == 0 else f"{stem} ({i}){suffix}")
        try:
            fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(fd)
            return p
        except FileExistsError:
            i += 1
            if i > 9999:
                # 抛 OSError 而不是 RuntimeError：convert_one 的兜底里能收住它，
                # 不会因为一个文件夹里同名文件太多就把整批转换崩掉。
                raise OSError(0, f"输出目录里同名文件过多：{filename}")


def _drop(path: Path | None) -> None:
    if path is None:
        return
    try:
        Path(path).unlink()
    except (OSError, FileNotFoundError):
        pass


# ---------------------------------------------------------------- 参数换算

def _resolve_kbps(bitrate: str, src_bitrate: float) -> tuple[float, str]:
    """把下拉选值换成数字；「保持原码率」读不到源码率时回落 192k。"""
    if bitrate in (None, "", "keep"):
        if src_bitrate and src_bitrate > 0:
            return float(src_bitrate), ""
        return BITRATE_FALLBACK, "源文件没写码率，已按 192k 输出"
    text = str(bitrate).lower().strip()
    if text.endswith("k"):
        text = text[:-1]
    try:
        value = float(text)
    except ValueError:
        value = BITRATE_FALLBACK
    if value <= 0:
        value = BITRATE_FALLBACK
    return value, ""


def _is_lossless_codec(codec: str) -> bool:
    """无损源的「码率」只是它压得紧不紧，不能拿来当有损目标的码率用。

    flac 往往只有 100k 左右，wav 则高达 1411k——直接沿用会得到又糊又离谱的结果，
    所以「保持原码率」遇到无损源要换成 192k 并在说明里写清楚。
    """
    return codec.startswith("pcm_") or codec in (
        "flac", "alac", "ape", "wavpack", "tta", "truehd")


def measured_kbps(src_bytes: int, duration: float) -> float:
    """用「体积 ÷ 时长」反推平均码率：容器里写的标称值经常不可信。"""
    if src_bytes <= 0 or duration <= 0:
        return 0.0
    return src_bytes * 8 / duration / 1000 * 0.99


def _quantize_kbps(value: float, dst_format: str) -> float:
    """落到编码器真正支持的档位：mp3 用固定阶梯向下取，其它格式取整。"""
    v = max(KBPS_MIN, min(KBPS_MAX, float(value)))
    if dst_format == "mp3":
        floor = MP3_LADDER[0]
        for step in MP3_LADDER:
            if step <= v:
                floor = step
        return float(floor)
    return float(int(v))


def _keep_kbps(reported: float, src_bytes: int, duration: float,
               codec: str, dst_format: str = "mp3") -> tuple[float, str]:
    """「保持原码率」的真实含义是「别让转完的文件反而变大」。

    ogg / Vorbis 这类可变码率文件，ffprobe 读到的是流头里的标称值，
    经常远高于真实平均码率（实测标称 500k、按体积只有 108k）。照抄标称值
    会把一个 863KB 的文件转成 2.5MB，所以标称明显虚高时按实测平均值输出，
    并封顶 192k——对语音和大部分音乐已经足够，不会比源文件更糊。
    """
    if _is_lossless_codec(codec):
        return BITRATE_FALLBACK, ("源文件是无损格式，没有可沿用的码率，已按 192k 输出"
                                  "（想要更高可以在码率里选 320k）")
    if not reported or reported <= 0:
        return BITRATE_FALLBACK, "源文件没写码率，已按 192k 输出"
    avg = measured_kbps(src_bytes, duration)
    if avg > 0 and reported > avg * VBR_SLACK and reported - avg >= 16:
        kbps = _quantize_kbps(min(avg, VBR_KEEP_CAP), dst_format)
        if avg < KBPS_MIN:
            return kbps, (f"源文件标称 {reported:.0f}k 偏高（按体积实测约 {avg:.0f}k），"
                          f"已按下限 {kbps:.0f}k 输出")
        return kbps, (f"源文件标称 {reported:.0f}k 偏高（按体积实测约 {avg:.0f}k），"
                      f"已按 {kbps:.0f}k 输出，避免转完反而变大")
    return _quantize_kbps(reported, dst_format), ""


def size_target_kbps(target_bytes: int, duration: float,
                     dst_format: str) -> float:
    """按目标体积反推码率：8 bit = 1 字节，留 SIZE_SAFETY 给封装开销。"""
    if target_bytes <= 0 or duration <= 0:
        return 0.0
    raw = target_bytes * 8 / duration / 1000 * SIZE_SAFETY
    return _quantize_kbps(raw, dst_format)


def _codec_matches(codec: str, dst_format: str) -> bool:
    """源编码是否已经和目标格式装得下（决定能不能 -c copy 直通）。"""
    if dst_format == "wav":
        return codec.startswith("pcm_")
    if dst_format == "ogg":
        return codec in ("vorbis", "opus")
    if dst_format == "m4a":
        return codec == "aac"
    if dst_format == "mp3":
        return codec == "mp3"
    if dst_format == "flac":
        return codec == "flac"
    return False


def _can_passthrough(info, dst_format: str, kbps: float,
                     start: float, end: float, gain: int, fade: float) -> bool:
    """没有任何处理、且码率没变，才允许直通。"""
    if not _codec_matches(info.codec, dst_format):
        return False
    if start > 0 or end > 0 or gain != 100 or fade > 0:
        return False
    if dst_format in LOSSLESS:
        return True
    if not info.bitrate:
        return False
    return abs(info.bitrate - kbps) <= max(4.0, kbps * 0.02)


# ---------------------------------------------------------------- 单条处理

def convert_one(src, dst_format: str = DEFAULT_TARGET,
                bitrate: str = DEFAULT_BITRATE, start: float = 0.0,
                end: float = 0.0, gain: int = 100, fade: float = 0.0,
                out_dir=None, on_progress=None,
                cancel: threading.Event | None = None,
                target_mb: float = 0.0) -> ItemReport:
    """转一条文件。任何异常都收进报告，不往外抛——批量里一条坏了不能拖垮整批。"""
    t0 = time.perf_counter()
    src = Path(src)
    r = ItemReport(src=str(src), dst_format=dst_format, start=float(start),
                   end=float(end), gain_percent=int(gain), fade=float(fade))
    reserved: Path | None = None
    try:
        if target_mb and float(target_mb) > 0:
            r.target_bytes = int(round(float(target_mb) * 1048576))
        r.src_bytes = src.stat().st_size
        r.src_sha256_before = _sha256(src)

        info = probe_audio(src)
        r.src_format = (f"{src.suffix.lstrip('.').lower()}(视频)"
                        if info.from_video else src.suffix.lstrip(".").lower())
        r.src_codec, r.src_bitrate = info.codec, info.bitrate
        r.src_sample_rate, r.src_channels = info.sample_rate, info.channels
        r.duration, r.from_video = info.duration, info.from_video

        # 1) 剪切参数校验：越界要给出可读原因，不静默截断
        if start < 0:
            r.note = "起始时间不能小于 0 秒"
            return _finish(src, r, t0)
        if start >= info.duration:
            r.note = (f"起始时间 {start:g} 秒超过了音频总时长"
                      f"（{info.duration:.1f} 秒），没有内容可转")
            return _finish(src, r, t0)
        if end > 0 and end <= start:
            r.note = (f"起始时间要小于结束时间（起始 {start:g} 秒，"
                      f"结束 {end:g} 秒）")
            return _finish(src, r, t0)
        if end > info.duration + 0.05:
            r.note = (f"结束时间 {end:g} 秒超过了音频总时长"
                      f"（{info.duration:.1f} 秒），请改小或填 0（到最后）")
            return _finish(src, r, t0)
        out_dur = (end - start) if end > 0 else (info.duration - start)
        r.out_duration = out_dur
        if out_dur <= 0.05:
            r.note = "剪切后没有内容了（不足 0.05 秒），换个时间段试试"
            return _finish(src, r, t0)

        notes: list[str] = []

        # 2) 码率：无损格式忽略码率；限体积时按目标反推；否则按选值或源码率
        target_bytes = r.target_bytes
        if dst_format in LOSSLESS:
            kbps = 0.0
            had_target = bool(target_bytes)
            target_bytes = 0
            r.target_bytes = 0
            if had_target:
                notes.append("无损格式不能靠调码率压体积，目标大小已忽略")
            elif bitrate not in (None, "", "keep"):
                notes.append(f"{dst_format} 是无损格式，码率设置已忽略（不影响音质）")
        elif target_bytes > 0:
            kbps = size_target_kbps(target_bytes, out_dur, dst_format)
            notes.append(f"按目标大小 {_mb(target_bytes)} 估算码率约 {kbps:.0f}k")
        else:
            kbps, fallback_note = _resolve_kbps(bitrate, info.bitrate)
            if bitrate in (None, "", "keep"):
                # 视频文件的总字节数里大部分是画面，拿它反推音频平均码率没有意义
                kbps, fallback_note = _keep_kbps(
                    info.bitrate, 0 if info.from_video else r.src_bytes,
                    info.duration, info.codec, dst_format)
            if kbps > KBPS_MAX:
                kbps = KBPS_MAX
                fallback_note = "码率上限是 320k，已按 320k 输出"
            if fallback_note:
                notes.append(fallback_note)
        r.target_kbps = kbps

        # 3) 淡入淡出：两头各占一份，音频太短就收回一半并说明
        fade_used = float(fade) if fade and fade > 0 else 0.0
        if fade_used > 0:
            if fade_used * 2 > out_dur:
                fade_used = max(0.05, round(out_dur / 2, 2))
                notes.append(f"音频太短，淡入淡出时长已收到 {fade_used:g} 秒")
        r.fade_used = fade_used

        out_dir = Path(out_dir) if out_dir else src.parent / OUT_DIRNAME
        out_dir.mkdir(parents=True, exist_ok=True)
        filename = src.stem + ENCODERS[dst_format][1]

        # 4) 直通：不重编码，只换封装（或原样复制）
        #    限体积时，只有源文件本来就在目标以内才允许直通；超了必须重编码压小。
        can_pass = _can_passthrough(
            info, dst_format, info.bitrate if target_bytes else kbps,
            start, end, gain, fade_used)
        if target_bytes and r.src_bytes > target_bytes:
            can_pass = False
        if can_pass:
            reserved = _reserve_path(out_dir, filename)
            rc, err = run_ffmpeg(
                ["-y", "-i", str(src), "-vn", "-map", "0:a:0", "-c:a", "copy",
                 str(reserved)],
                info.duration, on_progress, cancel)
            if rc != 0:
                _drop(reserved)
                reserved = None
                r.note = ("已取消" if (cancel is not None and cancel.is_set())
                          else describe_failure(rc, err))
                return _finish(src, r, t0)
            r.dst, r.ok = str(reserved), True
            r.passthrough, r.reencoded = True, False
            r.dst_codec = info.codec
            same_ext = src.suffix.lower() == ENCODERS[dst_format][1]
            notes.insert(0, "未重新编码：原音频流直接复制"
                            + ("" if same_ext else "（只换了封装格式）"))
            if target_bytes:
                notes.append(f"原文件 {_mb(r.src_bytes)} 已在目标大小以内，无需压缩")
            _fill_from_output(reserved, r)
            r.note = "；".join(notes)
            return _finish(src, r, t0)

        # 5) 重编码：按命令表拼参数，顺序固定 前置 → 剪切 → 滤镜 → 编码器
        encoder = ENCODERS[dst_format][0]
        r.dst_encoder = encoder
        filters: list[str] = []
        if gain != 100:
            filters.append(f"volume={gain / 100:g}")
        if fade_used > 0:
            filters.append(f"afade=t=in:st=0:d={fade_used:g}")
            filters.append(
                f"afade=t=out:st={max(0.0, out_dur - fade_used):.3f}:d={fade_used:g}")

        args = ["-y", "-i", str(src)]
        if start > 0:
            args += ["-ss", f"{start:.3f}"]
        if out_dur > 0:
            args += ["-t", f"{out_dur:.3f}"]
        args += ["-vn", "-map", "0:a:0"]
        if filters:
            args += ["-af", ",".join(filters)]

        def encode_args(rate: float) -> list[str]:
            out = list(args) + ["-c:a", encoder]
            if dst_format not in LOSSLESS:
                out += ["-b:a", f"{int(round(rate))}k"]
            return out

        # 限体积时，码率按公式估的可能还差一点点：超了就降一档重编一次，
        # 只重试一次，宁可如实汇报「略超」，也不无限压到听不清。
        attempts = 0
        while True:
            reserved = _reserve_path(out_dir, filename)
            rc, err = run_ffmpeg(encode_args(kbps) + [str(reserved)],
                                 out_dur, on_progress, cancel)
            if rc != 0:
                _drop(reserved)
                reserved = None
                r.note = ("已取消" if (cancel is not None and cancel.is_set())
                          else describe_failure(rc, err))
                return _finish(src, r, t0)
            size = reserved.stat().st_size
            if (target_bytes <= 0 or size <= target_bytes
                    or kbps <= KBPS_MIN or attempts >= 1):
                break
            attempts += 1
            lower = _quantize_kbps(kbps * target_bytes / size * 0.98, dst_format)
            if lower >= kbps:
                lower = _quantize_kbps(kbps * 0.8, dst_format)
            if lower >= kbps:
                break
            notes.append(f"第一遍 {_mb(size)} 超过目标，已降到 {lower:.0f}k 重编一次")
            _drop(reserved)
            reserved = None
            kbps = lower

        r.dst, r.ok = str(reserved), True
        r.passthrough, r.reencoded = False, True
        _fill_from_output(reserved, r)
        if dst_format in LOSSLESS:
            notes.insert(0, f"已转为 {dst_format}（无损格式，未做有损压缩）")
        elif target_bytes:
            over = r.dst_bytes > target_bytes
            notes.insert(0, f"已压到 {_mb(r.dst_bytes)}（约 {int(round(kbps))}k 有损重编码，"
                            f"目标 {_mb(target_bytes)}{'，略超' if over else ''}）")
            if over:
                notes.append("重编后仍略超目标：可把目标调大，或先用剪切缩短时长后再转")
        else:
            notes.insert(0, f"已转为 {dst_format}（约 {int(round(kbps))}k 有损重编码）")
        if r.dst_bytes > r.src_bytes and dst_format in LOSSLESS:
            notes.append("无损格式不压缩体积，比源文件大会是常态")
        r.note = "；".join(notes)
        return _finish(src, r, t0)
    except FFmpegMissing as exc:
        r.skipped, r.note = True, str(exc)
        return _finish(src, r, t0)
    except ValueError as exc:
        r.skipped, r.note = True, str(exc)
        return _finish(src, r, t0)
    except OSError as exc:
        detail = exc.strerror or str(exc)
        hint = "" if "同名文件过多" in detail else "（检查文件是否被占用或磁盘是否满）"
        r.note = f"读写失败：{detail}{hint}"
        return _finish(src, r, t0)
    finally:
        if reserved is not None and not r.ok:
            _drop(reserved)


def _fill_from_output(path: Path, r: ItemReport) -> None:
    """按产物实测值填报告：码率、采样率、声道、时长都以落地文件为准。"""
    r.dst_bytes = path.stat().st_size if path.is_file() else 0
    try:
        out_info = probe_audio(path)
    except (ValueError, FFmpegMissing, OSError):
        return
    r.dst_codec = out_info.codec or r.dst_codec
    r.dst_bitrate = out_info.bitrate
    r.dst_sample_rate = out_info.sample_rate
    r.dst_channels = out_info.channels
    r.out_duration = out_info.duration


def _finish(src: Path, r: ItemReport, t0: float) -> ItemReport:
    """收尾：复核原文件没被动过，记耗时。这条证据每次都要留。"""
    try:
        r.src_unchanged = (_sha256(src) == r.src_sha256_before)
    except OSError:
        r.src_unchanged = None
    r.elapsed = time.perf_counter() - t0
    return r


# ---------------------------------------------------------------- 批量

def convert_batch(paths, dst_format: str = DEFAULT_TARGET,
                  bitrate: str = DEFAULT_BITRATE, start: float = 0.0,
                  end: float = 0.0, gain: int = 100, fade: float = 0.0,
                  out_dir=None, on_item=None, progress_cb=None,
                  cancel: threading.Event | None = None,
                  target_mb: float = 0.0) -> BatchReport:
    """按顺序逐条转。音频编码很快，串行既好定位失败，进度条也不会失真。"""
    t0 = time.perf_counter()
    files = collect_audio(paths)
    report = BatchReport(dst_format=dst_format, bitrate=bitrate,
                         target_mb=float(target_mb or 0.0),
                         start=float(start), end=float(end),
                         gain=int(gain), fade=float(fade))
    highest = [0.0]

    def emit(value: float) -> None:
        if progress_cb is None or not files:
            return
        # 单条内部进度回退（例如重试）不能把整批进度条拉回去
        highest[0] = max(highest[0], min(1.0, value))
        progress_cb(highest[0])

    def per_item(frac: float) -> None:
        emit((len(report.items) + max(0.0, min(1.0, frac))) / len(files))

    for src in files:
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
        dest = Path(out_dir) if out_dir else src.parent / OUT_DIRNAME
        item = convert_one(src, dst_format, bitrate, start, end, gain, fade,
                           out_dir=dest, on_progress=per_item, cancel=cancel,
                           target_mb=target_mb)
        report.items.append(item)
        if item.ok and item.dst:
            d = str(Path(item.dst).parent)
            if d not in report.out_dirs:
                report.out_dirs.append(d)
        if on_item is not None:
            on_item(item)
        emit(len(report.items) / len(files))
        # 取消可能发生在最后一条身上：这时循环已经结束，靠「下一轮开头」是标不上的
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break
    report.elapsed = time.perf_counter() - t0
    return report


def write_reports(report: BatchReport) -> list[Path]:
    """把逐条报告写进每个 out 目录的 report.json（界面、CLI、自检共用）。"""
    if not report.out_dirs:
        return []
    payload = json.dumps(report.to_dict(), ensure_ascii=False, indent=2)
    written: list[Path] = []
    for d in report.out_dirs:
        try:
            p = Path(d) / "report.json"
            p.write_text(payload, encoding="utf-8")
            written.append(p)
        except OSError:
            pass
    return written
