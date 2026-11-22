"""ffmpeg / ffprobe 的定位、探测与调用（音频版）。

只做三件事：找到运行时装在哪儿、问清源文件是什么规格、跑一条命令并回报进度。
格式参数怎么拼、能不能直通，放 core/audio_job.py。

路径查找顺序按「离程序最近」排：打包目录 → exe 同级 → 源码树 → PATH → 本机开发路径。
这样开发时不用配环境变量，打包后也不会去调用用户机器上可能不存在的 ffmpeg。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

# 打包成窗口程序后，每次调 ffmpeg 都不许闪黑框
_NO_WINDOW = 0x08000000 if os.name == "nt" else 0

# 输出格式 → 实际调用的编码器名（本项目只提供这 5 种输出）
TARGET_ENCODERS = {
    "mp3": "libmp3lame",
    "m4a": "aac",
    "ogg": "libvorbis",
    "flac": "flac",
    "wav": "pcm_s16le",
}


class FFmpegMissing(RuntimeError):
    """找不到 ffmpeg：消息要直接告诉用户下一步做什么，不是甩一个路径错误。"""


@dataclass
class AudioInfo:
    """源文件规格。码率单位统一 kbps，时长统一秒。"""

    path: Path
    duration: float          # 秒
    codec: str               # mp3 / aac / flac / pcm_s16le / vorbis ...
    bitrate: float           # kbps；读不到记 0
    sample_rate: int         # Hz
    channels: int
    from_video: bool         # 视频文件，取的是第 1 条音轨
    fmt: str                 # 容器 format_name


_TOOLS: tuple[Path, Path] | None = None


def _candidates() -> list[Path]:
    """按优先级列出可能的 ffmpeg 所在目录。"""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):  # PyInstaller 解包目录
        base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        dirs += [base / "ffmpeg" / "bin", base / "ffmpeg", base]
        exe_dir = Path(sys.executable).parent
        dirs += [exe_dir / "ffmpeg" / "bin", exe_dir / "ffmpeg", exe_dir]
    here = Path(__file__).resolve().parent.parent
    dirs += [here / "ffmpeg" / "bin", here / "ffmpeg"]
    env = os.environ.get("AUDIO_CONVERT_FFMPEG")
    if env:
        p = Path(env)
        dirs.insert(0, p.parent if p.is_file() else p)
    # 本机开发用：优先项目自带的 ffmpeg，无需外部绝对路径
    return dirs


def find_tools() -> tuple[Path, Path]:
    """返回 (ffmpeg, ffprobe) 绝对路径；找不到抛 FFmpegMissing。"""
    global _TOOLS
    if _TOOLS is not None:
        return _TOOLS
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    probe_exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    for d in _candidates():
        f = d / exe
        if f.is_file():
            p = d / probe_exe
            if not p.is_file():
                p = Path(shutil.which("ffprobe") or "")
            if not p or not str(p) or not Path(p).is_file():
                continue
            _TOOLS = (f, Path(p))
            return _TOOLS
    for name in (exe, probe_exe):
        if shutil.which(name) is None:
            raise FFmpegMissing(
                "找不到 ffmpeg（转换音频需要它）。请把 ffmpeg 文件夹放在程序旁边，"
                "或重新下载完整安装包——不要只拷贝 exe 一个文件。")
    _TOOLS = (Path(shutil.which(exe)), Path(shutil.which(probe_exe)))
    return _TOOLS


def _run_capture(args: list[str],
                 timeout: float = 20.0) -> tuple[int, str, str]:
    """跑一次 ffprobe 之类的短命令。**必须带超时**：网络盘或损坏文件会让
    ffprobe 卡住，卡住的探测线程无法结束，Windows 关闭窗口时就会崩。"""
    try:
        proc = subprocess.run(
            args, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=_NO_WINDOW, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 1, "", f"读取超时（超过 {timeout:.0f} 秒没有响应，文件可能在网络盘上或已损坏）"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _last_line(text: str) -> str:
    lines = [x.strip() for x in (text or "").splitlines() if x.strip()]
    return lines[-1][:140] if lines else ""


def probe_audio(path: Path) -> AudioInfo:
    """读源文件规格。读不出来就抛 ValueError，由调用方记成「跳过」并说明原因。"""
    path = Path(path)
    # .m4p 是 iTunes 的 DRM 封装，ffmpeg 解不开，先给一句能看懂的话
    if path.suffix.lower() == ".m4p":
        raise ValueError("这个文件有版权保护（DRM 加密），本工具不处理")

    _, ffprobe = find_tools()
    rc, out, err = _run_capture([
        str(ffprobe), "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", str(path)])
    if rc != 0:
        low = (err or "").lower()
        if "encrypted" in low or "drm" in low or "protection" in low:
            raise ValueError("这个文件有版权保护（DRM 加密），本工具不处理")
        if "no such file" in low:
            raise ValueError("找不到这个文件，可能已被移动或删除")
        raise ValueError(f"读不出音频信息（可能已损坏或格式不支持）：{_last_line(err)}")
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise ValueError(f"读取音频信息失败：{exc}") from exc

    streams = data.get("streams") or []
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    if audio is None:
        raise ValueError("这个文件里没有音频轨道")

    video = next((s for s in streams
                  if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    fmt = data.get("format") or {}
    try:
        duration = float(fmt.get("duration") or audio.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    if duration <= 0:
        raise ValueError("读不出音频时长，无法处理这个文件")

    bitrate = 0.0
    try:
        bitrate = float(audio.get("bit_rate") or 0) / 1000
    except (TypeError, ValueError):
        pass
    if bitrate <= 0 and video is None:
        # 纯音频文件的容器码率就是音频码率；视频文件的容器码率含画面，不能拿来当音频码率
        try:
            bitrate = float(fmt.get("bit_rate") or 0) / 1000
        except (TypeError, ValueError):
            bitrate = 0.0

    try:
        sample_rate = int(audio.get("sample_rate") or 0)
    except (TypeError, ValueError):
        sample_rate = 0
    try:
        channels = int(audio.get("channels") or 0)
    except (TypeError, ValueError):
        channels = 0

    return AudioInfo(
        path=path, duration=duration,
        codec=(audio.get("codec_name") or "").lower(),
        bitrate=bitrate, sample_rate=sample_rate, channels=channels,
        from_video=video is not None,
        fmt=(fmt.get("format_name") or "").lower())


@lru_cache(maxsize=1)
def available_encoders() -> dict[str, str]:
    """返回 {目标格式: 实际编码器名}；缺哪个就在界面上把那个格式置灰。

    随包的 ffmpeg 里这 5 个编码器都有，这个检查是防御打包漏 DLL 的情况：
    真缺了要说出来，不能让用户转完才发现输出是空文件。
    """
    ffmpeg, _ = find_tools()
    rc, out, err = _run_capture([str(ffmpeg), "-hide_banner", "-encoders"])
    text = (out or "") + (err or "")
    found: dict[str, str] = {}
    for fmt, encoder in TARGET_ENCODERS.items():
        if re.search(rf"(^|\s){re.escape(encoder)}(\s|$)", text, re.MULTILINE):
            found[fmt] = encoder
    return found


def run_ffmpeg(args: list[str], total_seconds: float = 0.0,
               on_progress=None, cancel: threading.Event | None = None) -> tuple[int, str]:
    """跑一条 ffmpeg 命令，边跑边报进度；返回 (退出码, 最后几行错误)。

    进度来自 `-progress pipe:1` 的 out_time_us：它比解析 "size=..." 那种
    带 \\r 的输出可靠，也不会因为 loglevel 变化而失效。
    """
    ffmpeg, _ = find_tools()
    cmd = [str(ffmpeg), "-hide_banner", "-loglevel", "error", "-nostats",
           "-nostdin", "-progress", "pipe:1"] + args
    proc = subprocess.Popen(
        cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        creationflags=_NO_WINDOW)
    tail: deque[str] = deque(maxlen=60)

    def drain() -> None:
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    tail.append(line)
        except (ValueError, OSError):
            pass

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()

    killed = False
    try:
        for raw in proc.stdout:
            if cancel is not None and cancel.is_set() and not killed:
                # 直接跳出读循环去 wait/kill：继续读 stdout 有可能永远等不到 EOF，
                # 那样「取消」就会一直卡着，进度条也停不下来。
                proc.terminate()
                killed = True
                break
            raw = raw.strip()
            if not raw or "=" not in raw:
                continue
            key, _, value = raw.partition("=")
            if key in ("out_time_us", "out_time_ms") and total_seconds > 0:
                # 注意：ffmpeg 的 out_time_ms 实际也是微秒，历史命名遗留
                try:
                    seconds = int(value) / 1_000_000
                except ValueError:
                    continue
                if on_progress is not None:
                    on_progress(max(0.0, min(1.0, seconds / total_seconds)))
    finally:
        try:
            rc = proc.wait(timeout=15)
        except subprocess.TimeoutExpired:  # 极端情况下别把界面卡死
            proc.kill()
            rc = proc.wait()
        reader.join(timeout=3)
    return rc, "\n".join(tail)


def describe_failure(rc: int, stderr: str) -> str:
    """把 ffmpeg 的英文报错翻成一句人话 + 下一步动作。"""
    text = (stderr or "").lower()
    if "no space left" in text:
        return "磁盘空间不够，删点东西或换个输出位置再试"
    if "permission denied" in text:
        return "没有写入权限，换个文件夹，或先把同名文件关掉"
    if "invalid data found" in text or "moov atom not found" in text:
        return "这个音频文件可能不完整或已损坏，换一个文件试试"
    if "unknown encoder" in text:
        return "当前 ffmpeg 缺少音频编码器，请用完整安装包覆盖安装"
    if "does not contain any stream" in text:
        return "这个文件里没有可用的音频轨道，没法转"
    if "already exists" in text:
        return "输出文件已存在且无法覆盖，换个输出文件夹再试"
    detail = (stderr or "").strip().splitlines()
    last = detail[-1][:160] if detail else f"ffmpeg 退出码 {rc}"
    return f"转换失败：{last}"


def version_text() -> str:
    """用于自检与「使用说明」——确认打包后用的到底是哪个 ffmpeg。"""
    ffmpeg, _ = find_tools()
    rc, out, _ = _run_capture([str(ffmpeg), "-version"])
    first = (out or "").splitlines()[0] if out else ""
    return first if rc == 0 else f"读不到版本信息（{ffmpeg}）"
