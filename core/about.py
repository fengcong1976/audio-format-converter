"""软件身份信息：关于页、使用说明、命令行 --version、打包版本资源共用这一处定义。"""
from __future__ import annotations

APP_NAME = "音频格式转换工具"
APP_VERSION = "1.0.1"
APP_BUILD = "2026-11-12"
COMPANY = "深圳市创炫互动网络科技有限公司"

WEBSITE = "www.mrheedu.com"
HOMEPAGE = "https://www.mrheedu.com"

DISCLOSURE = (
    "mp3 / m4a / ogg 是有损格式：转进去就会重新压缩，高频细节会减少，"
    "码率越低越明显；flac / wav 之间互转不产生新的损失。"
    "原文件始终不动，结果写在原文件旁边的 out 文件夹里，"
    "逐条的码率变化见结果表，双击任意一行可以看完整披露。"
)

USAGE_SHORT = [
    "把音频或视频文件拖进窗口（也可以拖文件夹），选好输出格式，点「开始转换」",
    "支持 mp3 / wav / flac / m4a / aac / ogg / wma / ape / opus / aiff；"
    "视频文件会自动只取第 1 条音轨",
    "输出格式 5 种：mp3 / m4a / ogg（有损，可设码率）、flac / wav（无损，码率不生效）",
    "「保持原码率」按体积反推真实码率，ogg 这类可变码率不会越转越大；"
    "勾「限制大小」可填 MB 上限，码率自动反推，转完复称体积",
    "想只转一段就填「剪切」的起始与结束秒数；结束填 0 = 一直到最后",
    "音量 0%–200%（100% 不动）；淡入淡出填秒数，0 = 关",
    "结果在原文件旁边的 out 文件夹，原文件不动；同名结果自动加序号，绝不覆盖",
    "全程本地用内置 ffmpeg 处理，不联网、不上传",
]

VERSION_LINE = f"{APP_NAME} {APP_VERSION}（{APP_BUILD} 构建）· {COMPANY}"
VERSION_TEXT = f"{VERSION_LINE}\n更多免费软件：{WEBSITE}\n{DISCLOSURE}"
