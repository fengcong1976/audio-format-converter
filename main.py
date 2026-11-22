"""PySide6 桌面界面（需 pip install PySide6；CLI 与自检不依赖它）。

版式与站内其它转换工具保持一致：拖进来 → 选格式 → 开始；逐条报告；
结果落在原文件旁的 out 并自动打开；状态栏常驻 0 联网自检。

界面上不出现「编码器 / 采样率 / 容器」这类词：用户只需要说清「转成 mp3」，
怎么拼参数是程序的事，重编码了、码率怎么变，会在「说明」列和详情页里说明白。
原文件始终不动、结果只写在独立的 out 文件夹里，所以不设「撤销」按钮。

「关于」只放标准身份信息（应用名 / 版本 / 公司全称 / 网址）；
用法、有损披露与可自己核对的几件事单独放「使用说明」，两个入口不混在一起。
"""
from __future__ import annotations

import html
import sys
import threading
import time
from pathlib import Path

from PySide6.QtCore import Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices, QIcon, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QDialog,
    QDialogButtonBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QHeaderView,
    QLabel, QMainWindow, QMessageBox, QProgressBar, QPushButton, QSlider,
    QStyle, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.about import (
    APP_NAME, APP_VERSION, COMPANY, DISCLOSURE, HOMEPAGE, USAGE_SHORT, WEBSITE,
)
from core.audio_job import (
    BITRATE_CHOICES, DEFAULT_BITRATE, DEFAULT_TARGET, LOSSLESS, TARGETS,
    VIDEO_EXTS, collect_audio, convert_batch, write_reports,
)
from core.compliance import NetworkAudit
from core.ffmpeg_tool import (
    FFmpegMissing, available_encoders, probe_audio, version_text,
)


ICON_PATH = Path(__file__).resolve().parent / "resources" / "app.ico"
APP_USER_MODEL_ID = "Chuangxuan.AudioFormatConverter"
# 三处入口（选文件框 / 拖拽 / 状态栏）用同一句口径，免得互相打架。
SUPPORT_TEXT = ("mp3 / wav / flac / m4a / aac / ogg / wma / ape / opus / aiff"
                " ｜ 视频取音轨：mp4 / mkv / mov / avi 等")
FILE_FILTER = ("音频与视频 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma *.ape "
               "*.opus *.aiff *.mp4 *.mkv *.mov *.avi *.flv *.webm *.ts *.wmv *.m4v);;"
               "音频 (*.mp3 *.wav *.flac *.m4a *.aac *.ogg *.wma *.ape *.opus *.aiff);;"
               "视频 (*.mp4 *.mkv *.mov *.avi *.flv *.webm *.ts *.wmv *.m4v)")
PREVIEW_LIMIT = 300
EMPTY_HINT = "把音频或视频文件拖到这里（也可以拖文件夹）"

FORMAT_LABELS = {
    "mp3": "mp3（有损）",
    "m4a": "m4a（有损）",
    "ogg": "ogg（有损）",
    "flac": "flac（无损）",
    "wav": "wav（无损）",
}
BITRATE_LABELS = {
    "keep": "保持原码率",
    "320k": "320k",
    "192k": "192k",
    "128k": "128k",
    "64k": "64k",
}


def app_icon() -> QIcon:
    """图标文件缺失时返回空图标，不让界面启动失败。"""
    return QIcon(str(ICON_PATH)) if ICON_PATH.exists() else QIcon()


def set_windows_app_id() -> None:
    """让 Windows 任务栏显示本软件图标，而不是 python.exe 的图标。"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_USER_MODEL_ID)
    except Exception:
        pass


def configure_app(app: QApplication) -> None:
    """默认可读性：入口统一设定，界面里不提供字号开关。

    字放大后按钮、输入框、进度条、表格行高同步跟上，否则文字会挤在原来的格子里。
    """
    base_font = app.font()
    base_font.setPointSize(11)  # Windows 默认是 9pt
    app.setFont(base_font)
    app.setStyleSheet(
        "QPushButton { min-height: 34px; padding: 4px 12px; }"
        "QComboBox, QDoubleSpinBox, QSpinBox { min-height: 32px; padding: 2px 8px; }"
        "QProgressBar { min-height: 24px; }"
        "QCheckBox { padding: 2px 4px; }"
    )


def human(n: int) -> str:
    if n >= 1048576:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1024:.0f} KB"


def dur_text(seconds: float) -> str:
    """时长写成 03:12，一眼知道多长。"""
    if not seconds:
        return "—"
    s = int(round(seconds))
    return f"{s // 60:02d}:{s % 60:02d}"


class AboutDialog(QDialog):
    """「关于」只放身份信息，与站内其它项目的标准对话框一致。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"关于 {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(560, 300)

        root = QVBoxLayout(self)
        row = QHBoxLayout()
        pic = QLabel()
        pix = app_icon().pixmap(72, 72)
        if not pix.isNull():
            pic.setPixmap(pix)
        row.addWidget(pic)

        # 字段顺序与站内其它产品一致：应用名 → 版本 → 公司全称 → 更多免费软件
        info = QLabel(
            f"<h2>{html.escape(APP_NAME)}</h2>"
            f"<p>版本：{html.escape(APP_VERSION)}</p>"
            f"<p>公司：{html.escape(COMPANY)}</p>"
            f"<p>更多免费软件："
            f"<a href='{HOMEPAGE}'>{html.escape(WEBSITE)}</a></p>")
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setOpenExternalLinks(True)
        info.setWordWrap(True)
        row.addWidget(info, 1)
        root.addLayout(row)

        btns = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btns.accepted.connect(self.accept)
        root.addWidget(btns)


class UsageDialog(QDialog):
    """使用说明：几步用法 + 有损提示 + 一句可核对的事实，一屏看完。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"使用说明 · {APP_NAME}")
        self.setWindowIcon(app_icon())
        self.resize(660, 470)

        layout = QVBoxLayout(self)
        steps = "".join(f"<li>{html.escape(x)}</li>" for x in USAGE_SHORT)
        try:
            ff = html.escape(version_text())
        except FFmpegMissing as exc:
            ff = html.escape(str(exc))
        text = QLabel(
            f"<h3>怎么用</h3><ol>{steps}</ol>"
            f"<p><font color='#b3411c'>{html.escape(DISCLOSURE)}</font></p>"
            f"<p style='color:#666'>转完的文件在源文件旁边的 out 文件夹里，"
            f"源文件不会被删除、不会被改写；同名结果会自动加序号，绝不覆盖。"
            f"全程本地处理，不联网、不上传。</p>"
            f"<p style='color:#888;font-size:9pt'>内置转换引擎：{ff}</p>")
        text.setTextFormat(Qt.TextFormat.RichText)
        text.setWordWrap(True)
        layout.addWidget(text, 1)

        row = QHBoxLayout()
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class DetailDialog(QDialog):
    """单条结果的详情：要求逐条披露的字段一条不落。"""

    def __init__(self, item, parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"详情 · {Path(item.src).name}")
        self.setWindowIcon(app_icon())
        self.resize(700, 500)
        layout = QVBoxLayout(self)

        head = QLabel(
            f"<b>{html.escape(Path(item.src).name)}</b><br>"
            f"<span style='color:#666'>{html.escape(str(item.src))}</span>")
        head.setWordWrap(True)
        layout.addWidget(head)

        rows = item.disclosure()
        table = QTableWidget(len(rows), 2)
        table.setHorizontalHeaderLabels(["项目", "实际情况"])
        table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        table.setWordWrap(True)
        for r, (k, v) in enumerate(rows):
            table.setItem(r, 0, QTableWidgetItem(k))
            cell = QTableWidgetItem(v)
            cell.setToolTip(v)  # 长说明悬停看全文
            table.setItem(r, 1, cell)
        hh = table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        # 说明会换行，行高必须跟着文字走，否则第二行会被切掉
        table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents)
        table.horizontalHeader().setMinimumSectionSize(80)
        layout.addWidget(table, 1)

        row = QHBoxLayout()
        if item.dst and Path(item.dst).is_file():
            btn = QPushButton("打开结果所在文件夹")
            btn.clicked.connect(
                lambda: QDesktopServices.openUrl(
                    QUrl.fromLocalFile(str(Path(item.dst).parent))))
            row.addWidget(btn)
        row.addStretch()
        close = QPushButton("关闭")
        close.clicked.connect(self.accept)
        row.addWidget(close)
        layout.addLayout(row)


class ProbeWorker(QThread):
    """在后台把列表里每个文件的时长与码率读出来，逐条回填。

    几条就读几条，几百条会把界面冻住好几秒，所以放后台；只看到「3.2 MB」
    不知道是哪一条，有了时长和码率才敢确认没选错。
    """

    probed = Signal(str, float, float, str, bool)  # 路径、时长、码率、编码、来自视频

    def __init__(self, files):
        super().__init__()
        self.files = list(files)
        self._stop = threading.Event()

    def stop(self) -> None:
        self._stop.set()

    def run(self):
        for p in self.files:
            if self._stop.is_set():
                return
            try:
                info = probe_audio(Path(p))
            except Exception:  # 读不出来就留「—」，转换时会给出具体原因
                continue
            if self._stop.is_set():
                return
            self.probed.emit(str(p), info.duration, info.bitrate,
                             info.codec, info.from_video)


class Worker(QThread):
    item_done = Signal(object)     # 一条处理完就发一条，表格随进度补齐
    progressed = Signal(int, int)  # 已完成条数, 总条数
    # 不叫 finished：那是 QThread 自带的信号，同名不同签名会互相盖住。
    done = Signal(object, str)     # (BatchReport 或 None, 状态摘要)

    def __init__(self, files, dst_format, bitrate, start, end, gain, fade,
                 target_mb=0.0):
        super().__init__()
        self.files = list(files)
        self.dst_format = dst_format
        self.bitrate = bitrate
        self.target_mb = float(target_mb or 0.0)
        # 不能叫 self.start：QThread 自己就有 start()，一覆盖就再也起不来线程了
        self.trim_start = start
        self.trim_end = end
        self.gain = gain
        self.fade = fade
        self._cancel = threading.Event()

    def cancel(self) -> None:
        """请求取消：正在跑的 ffmpeg 会被掐掉，已转好的保留。"""
        self._cancel.set()

    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def run(self):
        # 这里漏出去的异常会让 PyQt 直接 abort 整个进程（窗口瞬间消失），
        # 所以整段兜住，失败也要给界面一个交代。
        try:
            with NetworkAudit() as audit:
                report = convert_batch(
                    self.files, dst_format=self.dst_format, bitrate=self.bitrate,
                    start=self.trim_start, end=self.trim_end, gain=self.gain,
                    fade=self.fade, target_mb=self.target_mb,
                    on_item=lambda it: self.item_done.emit(it),
                    progress_cb=self._on_progress,
                    cancel=self._cancel)
                report.network_attempts = audit.attempts
                write_reports(report)
                summary = audit.summary()
        except BaseException as exc:  # noqa: BLE001 — 兜底就是为了不让进程消失
            self.done.emit(
                None,
                f"转换中断：{type(exc).__name__}: {exc}。"
                "已经转好的文件仍在各自的 out 文件夹里，原文件未改动。")
            return
        self.done.emit(report, summary)

    def _on_progress(self, frac: float) -> None:
        total = len(self.files)
        if total <= 0:
            return
        # 向下取整：round 会让「2.9 / 3」提前变成「3 / 3 全部完成」，
        # 最后一条其实还在转。只有 frac 真到 1 才算全完成。
        done = int(frac * total)
        if frac < 1.0:
            done = min(done, total - 1)
        self.progressed.emit(max(0, min(total, done)), total)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} · 创炫互动")
        self.setWindowIcon(app_icon())
        self.resize(1120, 720)
        self.setAcceptDrops(True)
        self.paths: list[str] = []
        self.report = None
        self.worker = None
        self.row_index: dict[str, int] = {}  # 文件路径 → 表格行号，转换时原地更新
        self.preview_files: list = []
        # 用户在列表里删掉的行：只影响这批转哪些，原文件一个字节都不动。
        self.excluded: set[str] = set()
        self._running = False
        self._t0 = 0.0
        self.prober = None  # 后台预读时长 / 码率，换一批就停掉上一批
        self._stale: list = []  # 没能在超时内停下的旧线程，等它们自己结束后再释放

        # 找不到 ffmpeg 时界面照常打开，但开始按钮置灰并把原因写在拖拽区，
        # 不用等用户点了才弹一个「找不到 ffmpeg」的框。
        try:
            self.encoders = available_encoders()
            if not self.encoders:
                raise FFmpegMissing("内置 ffmpeg 里没有可用的音频编码器，请用完整安装包覆盖安装")
            self.ffmpeg_ok, self.ffmpeg_msg = True, ""
        except FFmpegMissing as exc:
            self.encoders = {}
            self.ffmpeg_ok, self.ffmpeg_msg = False, str(exc)

        root = QWidget()
        self.setCentralWidget(root)
        layout = QVBoxLayout(root)

        self.drop_label = QLabel(EMPTY_HINT if self.ffmpeg_ok else self.ffmpeg_msg)
        self.drop_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.drop_label.setWordWrap(True)
        self.drop_label.setStyleSheet(
            "padding: 28px; border: 1px dashed #888; border-radius: 8px;"
            + ("" if self.ffmpeg_ok else "color: #b3411c;"))
        layout.addWidget(self.drop_label)

        # ---------------- 第一行：选文件 / 开始 / 取消
        row = QHBoxLayout()
        # 转换中要把这两个按钮按住：转着的时候换清单，后台回填的行号就全错位了。
        self.btn_files = QPushButton("选择文件")
        self.btn_files.setToolTip(f"选一个或多个文件（{SUPPORT_TEXT}）。\n"
                                  "视频文件会自动只取第 1 条音轨。")
        self.btn_files.clicked.connect(self.pick_files)
        self.btn_dir = QPushButton("选择文件夹")
        self.btn_dir.setToolTip("选一整个文件夹，里面的音频与视频会连子文件夹一起找出来")
        self.btn_dir.clicked.connect(self.pick_dir)
        row.addWidget(self.btn_files)
        row.addWidget(self.btn_dir)
        row.addStretch()
        self.start_btn = QPushButton("开始转换")
        self.start_btn.setEnabled(self.ffmpeg_ok)
        self.start_btn.clicked.connect(self.start)
        row.addWidget(self.start_btn)
        self.cancel_btn = QPushButton("取消")
        self.cancel_btn.setEnabled(False)
        self.cancel_btn.setToolTip("掐掉正在转换的这一条，不再处理剩下的；已完成保留")
        self.cancel_btn.clicked.connect(self.cancel)
        row.addWidget(self.cancel_btn)
        layout.addLayout(row)

        # ---------------- 第二行：输出格式 + 码率
        fmt_row = QHBoxLayout()
        fmt_row.addWidget(QLabel("输出格式"))
        self.fmt = QComboBox()
        for key in TARGETS:
            label = FORMAT_LABELS.get(key, key)
            if key in self.encoders:
                self.fmt.addItem(label, key)
            else:
                self.fmt.addItem(f"{label}（缺少编码器）", key)
                model_item = self.fmt.model().item(self.fmt.count() - 1)
                if model_item is not None:
                    model_item.setEnabled(False)
        idx = self.fmt.findData(DEFAULT_TARGET)
        if idx >= 0 and DEFAULT_TARGET in self.encoders:
            self.fmt.setCurrentIndex(idx)
        self.fmt.setToolTip(
            "mp3 / m4a / ogg 是有损格式：转进去会重新压缩，高频细节会减少；\n"
            "flac / wav 是无损格式：不产生新的损失，但文件通常更大。")
        self.fmt.currentIndexChanged.connect(self.on_format_changed)
        fmt_row.addWidget(self.fmt)

        fmt_row.addSpacing(16)
        fmt_row.addWidget(QLabel("码率"))
        self.bitrate = QComboBox()
        for key in BITRATE_CHOICES:
            self.bitrate.addItem(BITRATE_LABELS.get(key, key), key)
        self.bitrate.setCurrentIndex(0)  # 保持原码率
        self.bitrate.setToolTip(
            "「保持原码率」：按源文件的实际码率走（体积反推，标称虚高不会照抄）；\n"
            "320k / 192k / 128k / 64k：只对 mp3 / m4a / ogg 生效。\n"
            "flac / wav 是无损格式，没有码率概念，这一行会置灰。")
        fmt_row.addWidget(self.bitrate)
        self.bitrate_hint = QLabel()
        self.bitrate_hint.setStyleSheet("color: #666;")
        fmt_row.addWidget(self.bitrate_hint)

        fmt_row.addSpacing(16)
        self.limit_size = QCheckBox("限制大小")
        self.limit_size.setToolTip(
            "勾选后按目标体积自动选码率，适合要给上传/发送留额度的情况。\n"
            "程序先用「目标体积 ÷ 时长」反推码率，转完复称体积；\n"
            "略微超出会自动降一档重编一次，仍超会如实写在说明里。")
        self.limit_size.toggled.connect(self.on_format_changed)
        fmt_row.addWidget(self.limit_size)
        self.size_limit = QDoubleSpinBox()
        self.size_limit.setRange(0.1, 2000)
        self.size_limit.setDecimals(1)
        self.size_limit.setSingleStep(1)
        self.size_limit.setSuffix(" MB")
        self.size_limit.setValue(8.0)
        self.size_limit.setMaximumWidth(120)
        self.size_limit.setToolTip("单个文件的大小上限；太小时码率会触底，音质会明显变差")
        fmt_row.addWidget(self.size_limit)
        fmt_row.addStretch()
        layout.addLayout(fmt_row)

        # ---------------- 第三行：剪切 + 音量 + 淡入淡出
        edit_row = QHBoxLayout()
        edit_row.addWidget(QLabel("剪切  从"))
        self.start_spin = QDoubleSpinBox()
        self.start_spin.setRange(0, 86400)
        self.start_spin.setDecimals(1)
        self.start_spin.setSingleStep(1)
        self.start_spin.setSuffix(" 秒")
        self.start_spin.setMaximumWidth(130)
        self.start_spin.setToolTip("从第几秒开始；0 = 从头开始")
        edit_row.addWidget(self.start_spin)
        edit_row.addWidget(QLabel("到"))
        self.end_spin = QDoubleSpinBox()
        self.end_spin.setRange(0, 86400)
        self.end_spin.setDecimals(1)
        self.end_spin.setSingleStep(1)
        self.end_spin.setSuffix(" 秒")
        self.end_spin.setMaximumWidth(130)
        self.end_spin.setToolTip("到第几秒结束；0 = 一直到最后。必须大于起始时间")
        edit_row.addWidget(self.end_spin)

        edit_row.addSpacing(16)
        edit_row.addWidget(QLabel("音量"))
        self.gain = QSlider(Qt.Orientation.Horizontal)
        self.gain.setRange(0, 200)
        self.gain.setValue(100)
        self.gain.setMinimumWidth(170)
        self.gain.setToolTip("100% = 不改动；200% = 声音放大一倍；0% = 静音。\n"
                             "只是放大已有的声音，不会凭空提升音质。")
        self.gain.valueChanged.connect(self.on_gain_changed)
        edit_row.addWidget(self.gain)
        self.gain_label = QLabel("100%")
        self.gain_label.setMinimumWidth(52)
        edit_row.addWidget(self.gain_label)

        edit_row.addSpacing(16)
        edit_row.addWidget(QLabel("淡入淡出"))
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0, 600)
        self.fade.setDecimals(1)
        self.fade.setSingleStep(0.5)
        self.fade.setSuffix(" 秒")
        self.fade.setMaximumWidth(120)
        self.fade.setToolTip("开头淡入、结尾淡出各占这么多秒；0 = 关闭。\n"
                             "音频比这个时长还短时会自动收小，并在说明里写清。")
        edit_row.addWidget(self.fade)

        self.open_after = QCheckBox("转换后打开结果目录")
        self.open_after.setChecked(True)
        edit_row.addSpacing(12)
        edit_row.addWidget(self.open_after)
        edit_row.addStretch()
        layout.addLayout(edit_row)

        # ---------------- 列表工具行：删行 / 清空只动「这批转哪些」
        list_row = QHBoxLayout()
        self.delete_btn = QPushButton("删除选中")
        self.delete_btn.setEnabled(False)
        self.delete_btn.setToolTip(
            "把选中的行移出待转换列表（原文件不动，随时可以重新选回来）。\n"
            "按住 Ctrl 点选多行，或按住 Shift 选一段；也可以直接按 Delete 键。")
        self.delete_btn.clicked.connect(lambda: self.delete_selected())
        list_row.addWidget(self.delete_btn)
        self.clear_btn = QPushButton("清空列表")
        self.clear_btn.setEnabled(False)
        self.clear_btn.setToolTip("清空整个待转换列表，不会删除、不会改动任何文件")
        self.clear_btn.clicked.connect(self.clear_list)
        list_row.addWidget(self.clear_btn)
        list_row.addSpacing(8)
        list_note = QLabel("只从清单移除，不会删除任何文件")
        list_note.setStyleSheet("color: #666;")
        list_row.addWidget(list_note)
        list_row.addStretch()
        layout.addLayout(list_row)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFormat("尚未开始")
        self.progress.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.progress.setStyleSheet(
            "QProgressBar { border: 1px solid #b9c6d6; border-radius: 4px;"
            " background: #f2f5f9; text-align: center; }"
            "QProgressBar::chunk { background-color: #2f7de1; border-radius: 3px; }")
        layout.addWidget(self.progress)

        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(
            ["文件", "格式", "码率", "时长", "大小", "状态", "说明"])
        # 整行选中才好认「要删哪几条」；默认是按单元格选，选出来是虚线框。
        self.table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(
            QAbstractItemView.SelectionMode.ExtendedSelection)
        # 单元格只是结果展示，双击进去改字没有意义；双击改成看详情。
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # 选中行必须一眼可见：Windows 默认的「失焦高亮」几乎就是白底。
        self.table.setStyleSheet(
            "QTableWidget::item:selected "
            "{ background-color: #0b64c4; color: #ffffff; }")
        self.table.setWordWrap(True)
        self.table.horizontalHeader().setMinimumSectionSize(80)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)  # 文件名最长，给足宽度
        for c in range(1, 6):
            hh.setSectionResizeMode(c, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)
        self._sync_table_fonts()
        self.table.itemSelectionChanged.connect(self._refresh_list_buttons)
        self.table.cellDoubleClicked.connect(self.show_detail)
        # Delete 键等价于点「删除选中」：手不离键盘也能清掉选错的那几行。
        self.del_shortcut = QShortcut(QKeySequence("Delete"), self.table)
        self.del_shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
        self.del_shortcut.activated.connect(lambda: self.delete_selected(hint=False))
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        self.net_label = QLabel("本次尚未运行 · 全程本地处理" if self.ffmpeg_ok
                                else "缺少 ffmpeg：无法转换")
        bottom.addWidget(self.net_label)
        bottom.addStretch()
        btn_open = QPushButton("打开结果文件夹")
        btn_open.setToolTip("结果就在每个文件旁边的 out 文件夹里；转完会自动打开一次")
        btn_open.clicked.connect(lambda: self.open_result_folder())
        bottom.addWidget(btn_open)
        btn_usage = QPushButton("使用说明")
        btn_usage.setToolTip("怎么用、结果在哪、有损说明、可自己核对的几件事")
        btn_usage.clicked.connect(self.show_usage)
        bottom.addWidget(btn_usage)
        btn_about = QPushButton("关于")
        btn_about.setToolTip("版本 / 公司全称 / 官网网址")
        btn_about.clicked.connect(self.show_about)
        bottom.addWidget(btn_about)
        layout.addLayout(bottom)

        self.on_format_changed()

    # ------------------------------------------------------------ 字体与宽度
    def _fit_combo_width(self, combo: QComboBox) -> None:
        """下拉框最小宽度按最长条目的实测文字宽算，不交给 Qt 的 sizeHint。"""
        fm = combo.fontMetrics()
        text_w = max(
            (fm.horizontalAdvance(combo.itemText(i)) for i in range(combo.count())),
            default=0)
        arrow = combo.style().pixelMetric(
            QStyle.PixelMetric.PM_MenuButtonIndicator, None, combo)
        combo.setMinimumWidth(text_w + arrow + 34)  # 34 = 左右内边距 + 余量

    def _sync_table_fonts(self) -> None:
        """表格正文与表头统一到全局字号（11pt）。"""
        font = QApplication.font()
        for w in (self.table, self.table.horizontalHeader(),
                  self.table.verticalHeader()):
            w.setFont(font)
        self.table.verticalHeader().setDefaultSectionSize(
            self.table.fontMetrics().height() + 14)
        self._fit_row_number_width()

    def _fit_row_number_width(self) -> None:
        """行号列宽按实际字号和位数算，避免两位数被切掉一半。"""
        vh = self.table.verticalHeader()
        digits = max(2, len(str(max(1, self.table.rowCount()))))
        fm = vh.fontMetrics()
        vh.setFixedWidth(fm.horizontalAdvance("0") * digits + 16)

    def showEvent(self, event):
        super().showEvent(event)
        self._sync_table_fonts()  # 样式刷新会把表头字体打回 9pt，显示后补一次
        self._fit_combo_width(self.fmt)
        self._fit_combo_width(self.bitrate)

    # ------------------------------------------------------------ 参数联动
    def current_format(self) -> str:
        data = self.fmt.currentData()
        return str(data) if data else DEFAULT_TARGET

    def current_bitrate(self) -> str:
        data = self.bitrate.currentData()
        return str(data) if data else DEFAULT_BITRATE

    def current_target_mb(self) -> float:
        """勾了「限制大小」才返回目标体积，否则 0 = 按码率走。"""
        if not self.limit_size.isChecked():
            return 0.0
        return float(self.size_limit.value())

    def on_format_changed(self, _index=None) -> None:
        """无损格式没有码率概念；勾了限制大小，码率改由目标体积自动算。"""
        fmt = self.current_format()
        lossless = fmt in LOSSLESS
        if lossless and self.limit_size.isChecked():
            # 无损格式没法靠码率压体积：自动取消勾选，避免点了没反应
            self.limit_size.blockSignals(True)
            self.limit_size.setChecked(False)
            self.limit_size.blockSignals(False)
        limiting = self.limit_size.isChecked()
        self.bitrate.setEnabled(not lossless and not limiting and not self._running)
        self.limit_size.setEnabled(not lossless and not self._running)
        self.size_limit.setEnabled(limiting and not lossless and not self._running)
        if lossless:
            hint, tip = "无损格式，码率不生效", (
                f"{fmt} 是无损格式：不产生新的损失，也没有「码率」可调，选中的码率会被忽略。")
        elif limiting:
            hint, tip = "已按目标大小自动选码率", "码率由「目标大小 ÷ 时长」反推，不用手选"
        else:
            hint, tip = "", ""
        self.bitrate_hint.setText(hint)
        self.bitrate_hint.setToolTip(tip)
        self._fit_combo_width(self.bitrate)

    def on_gain_changed(self, value: int) -> None:
        self.gain_label.setText(f"{value}%")
        color = "#b3411c" if value != 100 else "#000"
        self.gain_label.setStyleSheet(f"color: {color};")

    # ------------------------------------------------------------ 选文件
    def dragEnterEvent(self, event):
        if event.mimeData().hasUrls() and not self._running:
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.paths = [u.toLocalFile() for u in event.mimeData().urls() if u.toLocalFile()]
        self.excluded.clear()  # 换了新一批文件，上一批删掉的行不再挡路
        self.preview_selection()

    def pick_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "选择文件", "", FILE_FILTER)
        if files:
            self.paths = files
            self.excluded.clear()
            self.preview_selection()

    def pick_dir(self):
        d = QFileDialog.getExistingDirectory(self, "选择文件夹")
        if d:
            self.paths = [d]
            self.excluded.clear()
            self.preview_selection()

    # ------------------------------------------------------------ 清单
    @staticmethod
    def _key(path) -> str:
        try:
            return str(Path(path).resolve())
        except OSError:
            return str(path)

    def preview_selection(self) -> list:
        """选中 / 拖入后先把文件列进表格：文件名、原体积、待转换。

        只读文件大小（stat），不探测时长（每条要起一次 ffprobe，几百条会卡）；
        时长与码率在后台预读后回填。
        """
        files = [p for p in collect_audio(self.paths)
                 if self._key(p) not in self.excluded]
        self.preview_files = files
        self.row_index = {}
        shown = files[:PREVIEW_LIMIT]
        self.table.setUpdatesEnabled(False)
        try:
            self.table.setRowCount(len(shown))
            for r, p in enumerate(shown):
                try:
                    size = p.stat().st_size
                except OSError:
                    size = 0
                src_fmt = p.suffix.lstrip(".").lower()
                if src_fmt in {x.lstrip(".") for x in VIDEO_EXTS}:
                    src_fmt += "(视频)"
                values = [p.name, src_fmt, "—", "—",
                          human(size) if size else "—", "待转换", ""]
                for c, v in enumerate(values):
                    cell = QTableWidgetItem(str(v))
                    if c == 0:
                        cell.setToolTip(str(p))  # 不同文件夹的重名文件，悬停看完整路径
                        cell.setData(Qt.ItemDataRole.UserRole, str(p))
                    self.table.setItem(r, c, cell)
                self.row_index[self._key(p)] = r
        finally:
            self.table.setUpdatesEnabled(True)
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._start_prober(files)
        if files:
            n = f"已加入 {len(files)} 个待转换文件 · 点「开始转换」即可"
            self.drop_label.setText(n)
            n = f"已选 {len(files)} 个文件 ｜ 还没开始 · 全程本地处理"
            if len(files) > PREVIEW_LIMIT:
                n += f" ｜ 列表只显示前 {PREVIEW_LIMIT} 个，其余照转"
            self.net_label.setText(n)
        elif self.ffmpeg_ok:
            self.drop_label.setText(EMPTY_HINT)
            self.net_label.setText(f"没有找到可转换的文件 ｜ 支持 {SUPPORT_TEXT}")
        return files

    def _start_prober(self, files) -> None:
        """换一批就停掉上一批的预读，避免旧结果填到新列表上。"""
        self._stop_prober()
        if not files or not self.ffmpeg_ok:
            return
        self.prober = ProbeWorker(files[:PREVIEW_LIMIT])
        self.prober.probed.connect(self.on_probed)
        self.prober.start()

    def _stop_prober(self) -> None:
        """停掉预读线程。**绝不销毁还在跑的 QThread**：pyqt 会在析构时报
        “QThread: Destroyed while thread is still running”，随后整个程序崩掉。
        等不到就挂到 _stale 里，等它自己 finished 再释放。"""
        prober = self.prober
        if prober is None:
            return
        self.prober = None
        prober.stop()
        if prober.isRunning() and not prober.wait(3000):
            self._stale.append(prober)
            prober.finished.connect(lambda p=prober: self._reap_thread(p))
            return
        prober.deleteLater()

    def _reap_thread(self, thread) -> None:
        """迟到的线程结束时再销毁；先把它从挂起清单里摘掉。"""
        try:
            self._stale.remove(thread)
        except ValueError:
            pass
        thread.deleteLater()

    def on_probed(self, path: str, duration: float, bitrate: float,
                  codec: str, from_video: bool) -> None:
        """时长与码率回填到「待转换」的行上；已经转出结果的行不再覆盖。"""
        if self._running:
            return
        r = self.row_index.get(self._key(path))
        if r is None or r >= self.table.rowCount():
            return
        status_cell = self.table.item(r, 5)
        if status_cell is None or status_cell.text() != "待转换":
            return
        self.table.item(r, 3).setText(dur_text(duration))
        if bitrate:
            self.table.item(r, 2).setText(f"{bitrate:.0f} kbps")
        if from_video:
            self.table.item(r, 1).setText(
                f"{Path(path).suffix.lstrip('.').lower()}(视频)")
        cell = self.table.item(r, 1)
        if cell is not None:
            cell.setToolTip(f"源编码：{codec or '未知'}"
                            + ("（视频文件，取第 1 条音轨）" if from_video else ""))

    def _row_path(self, row: int):
        """行 → 文件路径：从单元格里取，删过行也不会错位。"""
        cell = self.table.item(row, 0)
        return cell.data(Qt.ItemDataRole.UserRole) if cell is not None else None

    def _rebuild_row_index(self) -> None:
        self.row_index = {}
        for r in range(self.table.rowCount()):
            p = self._row_path(r)
            if p:
                self.row_index[self._key(p)] = r

    def _refresh_list_buttons(self) -> None:
        """删除 / 清空只在「有行、且没在转」时可用，免得转到一半表被抽走。"""
        has_row = self.table.rowCount() > 0
        has_pick = bool(self.table.selectedIndexes())
        self.delete_btn.setEnabled(has_pick and not self._running)
        self.clear_btn.setEnabled(has_row and not self._running)

    def _after_list_changed(self, n: int, verb: str) -> None:
        """删行 / 清空后同步拖拽区、状态栏与进度，不说一句「原文件未改动」不放心。"""
        left = len(self.preview_files)
        if left:
            self.drop_label.setText(f"待转换 {left} 个文件 · 点「开始转换」即可")
            self.net_label.setText(f"{verb} {n} 个 ｜ 还剩 {left} 个待转换 ｜ 原文件未改动")
        else:
            self.drop_label.setText(EMPTY_HINT)
            self.net_label.setText(f"{verb} {n} 个 ｜ 列表已空 ｜ 原文件未改动")
        self.progress.setValue(0)
        self.progress.setFormat("尚未开始")

    def delete_selected(self, hint: bool = True) -> None:
        """把选中行移出待转换列表：只是这批不转它们，原文件不删不改。"""
        if self._running:
            return
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        if not rows:
            if hint:
                QMessageBox.information(
                    self, "先选中行",
                    "在列表里点一下要删的那行再点「删除选中」。\n\n"
                    "按住 Ctrl 点击可多选，按住 Shift 可选一段，"
                    "选中后直接按 Delete 键也行。")
            return
        removed = 0
        self.table.setUpdatesEnabled(False)
        try:
            for r in rows:
                p = self._row_path(r)
                if p:
                    self.excluded.add(self._key(p))
                    removed += 1
                self.table.removeRow(r)
        finally:
            self.table.setUpdatesEnabled(True)
        self.preview_files = [p for p in self.preview_files
                              if self._key(p) not in self.excluded]
        self._rebuild_row_index()
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._after_list_changed(removed, "已移除")

    def clear_list(self) -> None:
        """清空整个待转换列表：不删文件、不动文件，只是这批不转了。"""
        if self._running:
            return
        rows = self.table.rowCount()
        if not rows and not self.paths:
            return
        if rows:
            ret = QMessageBox.question(
                self, "清空列表",
                f"把列表里这 {rows} 个文件都清掉吗？\n\n"
                "只是不再转换它们：原文件不会被删除、不会被改动，"
                "之后重新选择即可。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                return
        self.paths = []
        self.excluded.clear()
        self.preview_files = []
        self.row_index = {}
        self.table.setRowCount(0)
        self._fit_row_number_width()
        self._refresh_list_buttons()
        self._after_list_changed(rows, "已清空")

    # ------------------------------------------------------------ 转换
    def cancel(self):
        """掐掉正在转换的这一条：已完成的保留，界面上如实说明。"""
        if self.worker is not None:
            self.worker.cancel()
            self.cancel_btn.setEnabled(False)
            self.net_label.setText("正在停止：掐掉当前这条，已完成的会保留…")

    def _set_running(self, running: bool) -> None:
        if running:
            self._stop_prober()  # 转换要紧，预读先让位
        self._running = running
        self.start_btn.setEnabled(not running and self.ffmpeg_ok)
        self.cancel_btn.setEnabled(running)
        self.setAcceptDrops(not running)
        for w in (self.fmt, self.start_spin, self.end_spin, self.gain, self.fade,
                  self.btn_files, self.btn_dir):
            w.setEnabled(not running)
        self.on_format_changed()  # 码率 / 限制大小按格式与运行状态重新置灰
        self._refresh_list_buttons()

    def _reset_rows(self, files) -> bool:
        """开转前把已列出的行清回「待转换」，避免上一批的结果留在表里。"""
        shown = files[:PREVIEW_LIMIT]
        if self.table.rowCount() != len(shown):
            return False
        self.table.setUpdatesEnabled(False)
        try:
            for r in range(len(shown)):
                for c, v in ((2, "—"), (5, "待转换"), (6, "")):
                    self.table.setItem(r, c, QTableWidgetItem(v))
        finally:
            self.table.setUpdatesEnabled(True)
        return True

    def start(self):
        if self._running:
            return
        if not self.ffmpeg_ok:
            QMessageBox.critical(self, "缺少 ffmpeg", self.ffmpeg_msg)
            return
        if not self.paths:
            QMessageBox.information(self, "提示", "请先拖入文件，或选择文件 / 文件夹")
            return
        start = float(self.start_spin.value())
        end = float(self.end_spin.value())
        if end > 0 and end <= start:
            QMessageBox.warning(
                self, "时间段不对",
                f"结束时间（{end:g} 秒）要大于起始时间（{start:g} 秒）。\n\n"
                "想从起始一直转到最后，把结束填 0 即可。")
            return
        fmt = self.current_format()
        if fmt not in self.encoders:
            QMessageBox.critical(
                self, "缺少编码器",
                f"内置 ffmpeg 缺少 {fmt} 编码器，换个输出格式，"
                "或用完整安装包覆盖安装。")
            return
        # 复用上次预览的结果，不再重复扫描同一批目录
        files = self.preview_files
        if not files or not self._reset_rows(files):
            files = self.preview_selection()
        self.report = None
        self.progress.setValue(0)
        if not files:
            if self.excluded:
                QMessageBox.information(
                    self, "列表是空的",
                    "列表里的文件都被删掉了。\n\n"
                    "重新选择文件，或点「清空列表」从头来一遍即可。")
            else:
                QMessageBox.information(
                    self, "没有找到文件",
                    f"选中的位置里没有可转换的文件（{SUPPORT_TEXT}）。")
            return
        bitrate = "keep" if fmt in LOSSLESS else self.current_bitrate()
        target_mb = self.current_target_mb()
        self._set_running(True)
        self._t0 = time.perf_counter()
        self.progress.setFormat(f"0 / {len(files)}")
        how = ("，无损" if fmt in LOSSLESS else
               (f"，每个不超过 {target_mb:g} MB" if target_mb
                else f"，{BITRATE_LABELS.get(bitrate, bitrate)}"))
        self.net_label.setText(
            f"正在转换 {len(files)} 个文件（转为 {fmt}{how}）…")
        # 传的是「过滤后的文件清单」而不是原始路径：用户删掉的行不该再被转。
        self.worker = Worker(files, fmt, bitrate, start, end,
                             int(self.gain.value()), float(self.fade.value()),
                             target_mb)
        self.worker.item_done.connect(self.add_row)
        self.worker.progressed.connect(self.on_progress)
        self.worker.done.connect(self.on_finished)
        self.worker.start()

    def on_progress(self, done: int, total: int) -> None:
        """进度 + 预计剩余：按已完成条数估，不做假动作。"""
        if total <= 0:
            return
        pct = max(0, min(100, int(done * 100 / total)))
        self.progress.setValue(pct)
        elapsed = time.perf_counter() - self._t0
        if done <= 0:
            text = f"0 / {total} · 预计剩余估算中"
        elif done >= total:
            text = f"{total} / {total} · 全部完成"
        else:
            eta = elapsed / done * (total - done)
            text = f"{done} / {total} · 剩余约 {eta:.0f} 秒"
        self.progress.setFormat(f"{text} · {pct}%")

    def show_detail(self, row: int, _col: int = 0):
        """双击一行看完整披露：结果表列宽有限，只放得下半句说明。"""
        cell = self.table.item(row, 0)
        item = cell.data(Qt.ItemDataRole.UserRole + 1) if cell is not None else None
        if item is None:
            return
        DetailDialog(item, self).exec()

    @staticmethod
    def _rate_text(item) -> str:
        if not item.ok:
            return "—"
        if item.dst_bitrate:
            return f"{item.src_bitrate:.0f} → {item.dst_bitrate:.0f} kbps"
        return f"{item.src_bitrate:.0f} → —"

    @staticmethod
    def _short_note(item) -> str:
        """说明列用短句，完整说法放悬停与双击详情里。"""
        if not item.ok:
            return item.note or "—"
        bits = []
        if item.passthrough:
            bits.append("未重新编码（原音频直通）")
        elif item.dst_format in LOSSLESS:
            bits.append("无损格式")
        else:
            bits.append(f"有损重编码 {item.dst_bitrate:.0f}k")
        if item.target_bytes:
            bits.append(f"目标 ≤{item.target_bytes / 1048576:.1f}MB")
        if item.start > 0 or item.end > 0:
            bits.append(f"剪切 {item.out_duration:.1f}s")
        if item.gain_percent != 100:
            bits.append(f"音量 {item.gain_percent}%")
        if item.fade_used > 0:
            bits.append(f"淡入淡出 {item.fade_used:g}s")
        if item.from_video:
            bits.append("来自视频")
        return "；".join(bits)

    def add_row(self, item):
        key = self._key(item.src)
        r = self.row_index.get(key)
        if r is None:  # 不在预览列表里（超出预览上限等）才追加
            r = self.table.rowCount()
            self.table.insertRow(r)
            self.row_index[key] = r
            self._fit_row_number_width()
        if item.skipped:
            flag = "跳过"
        elif item.ok:
            flag = "完成（直通）" if item.passthrough else "完成"
        else:
            flag = "失败"
        values = [
            Path(item.src).name,
            (f"{item.src_format} → {item.dst_format}"
             if item.src_format else "—"),
            self._rate_text(item),
            dur_text(item.out_duration if item.ok else item.duration),
            (f"{human(item.src_bytes)} → {human(item.dst_bytes)}"
             if item.ok and item.dst_bytes else human(item.src_bytes)
             if item.src_bytes else "—"),
            flag,
            self._short_note(item),
        ]
        for c, v in enumerate(values):
            cell = QTableWidgetItem(str(v))
            if c == 0:
                cell.setData(Qt.ItemDataRole.UserRole, str(item.src))
                cell.setData(Qt.ItemDataRole.UserRole + 1, item)  # 详情页用
                cell.setToolTip(f"{item.src}\n双击这一行看完整详情")
            if c == 6:
                cell.setToolTip(item.note or str(v))
            self.table.setItem(r, c, cell)
        self.table.resizeRowToContents(r)

    def on_finished(self, report, summary):
        self._set_running(False)
        worker, self.worker = self.worker, None
        if worker is not None:
            # done 是在 run() 末尾发的，线程通常马上结束；万一还在收尾，
            # 继续等或挂起来，绝不对运行中的 QThread 调 deleteLater。
            if not worker.wait(2000):
                self._stale.append(worker)
                worker.finished.connect(lambda w=worker: self._reap_thread(w))
            else:
                worker.deleteLater()
        if report is None:  # Worker 兜底捕获的异常：必须让用户看见原因
            self.progress.setFormat("中断")
            self.net_label.setText(summary)
            QMessageBox.critical(self, "转换中断", summary)
            return
        self.report = report
        if report.cancelled:
            self.progress.setFormat("已取消")
        else:
            self.progress.setValue(100)
            self.progress.setFormat("完成 100%")
        parts = [summary]
        if report.cancelled:
            parts.insert(0, f"已取消（完成 {len(report.items)} 个，其余未处理）")
        parts.append(f"完成 {len(report.succeeded)} / {len(report.items)}")
        if report.passthrough:
            parts.append(f"其中直通（未重新编码）{len(report.passthrough)} 个")
        if report.skipped:
            parts.append(f"跳过 {len(report.skipped)} 个（原因见说明列）")
        if report.failed:
            parts.append("失败项的原因见说明列，双击可看完整详情")
        if not report.out_dirs:
            parts.append("本次没有生成结果文件")
        self.net_label.setText(" ｜ ".join(parts))
        self.net_label.setToolTip(self.net_label.text())
        if report.out_dirs and self.open_after.isChecked():
            self.open_result_folder(auto=True)

    # ------------------------------------------------------------ 打开结果
    def result_dirs(self) -> list[Path]:
        if not self.report:
            return []
        return [Path(d) for d in self.report.out_dirs if Path(d).is_dir()]

    def open_result_folder(self, auto: bool = False) -> None:
        """打开结果文件夹：转完自动开一次，也可随时点底部按钮再开。"""
        dirs = self.result_dirs()
        if not dirs:
            if not auto:
                QMessageBox.information(self, "提示", "还没有生成结果；先转换一次吧")
            return
        ok = QDesktopServices.openUrl(QUrl.fromLocalFile(str(dirs[0])))
        if not ok and not auto:
            QMessageBox.information(self, "打不开结果文件夹", f"请手动打开：{dirs[0]}")
        extra = f"（另有 {len(dirs) - 1} 个结果文件夹）" if len(dirs) > 1 else ""
        if auto:
            self.net_label.setText(self.net_label.text() + f" ｜ 结果已打开{extra}")
            self.net_label.setToolTip(self.net_label.text())

    def show_about(self):
        AboutDialog(self).exec()

    def show_usage(self):
        UsageDialog(self).exec()

    def closeEvent(self, event):
        """转换中关窗口：先问一句，并让后台线程收尾，避免 QThread 被销毁崩掉。"""
        if self.worker is not None and self.worker.isRunning():
            ret = QMessageBox.question(
                self, "还在转换",
                "还有文件正在转换。停止并退出吗？\n\n"
                "已经转好的结果会保留在原文件旁的 out 文件夹里，原文件不受影响；"
                "正在转换的这一条会被掐掉，不会留下半截文件。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if ret != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.worker.cancel()
            if not self.worker.wait(3000):
                QMessageBox.warning(
                    self, "请稍候",
                    "后台还在收尾，请几秒后再关闭窗口，以免留下半截文件。")
                event.ignore()
                return
        self._stop_prober()
        event.accept()


def run_selftest(out: Path | None = None) -> int:
    """无界面自检：确认「这一份 exe 里」ffmpeg 能用、能转、能直通。

    为什么打包后还要单独跑一遍：开发机上永远能找到 PATH 里的 ffmpeg，
    exe 却只认自己旁边那份。这条自检现场合成一段音频，用包内 ffmpeg 走完
    探测 → 转换 → 直通判断，输出写日志文件（窗口程序没有控制台），
    安装包验收时直接看这个文件就行。

    用法：音频格式转换工具.exe --selftest [日志路径]
    """
    import hashlib
    import tempfile

    from core.audio_job import convert_one
    from core.ffmpeg_tool import find_tools, run_ffmpeg

    lines: list[str] = []
    failed = 0

    def log(msg: str) -> None:
        lines.append(msg)
        try:  # 源码方式跑的时候顺便打到控制台；打包后这行只是无用的 no-op
            print(msg, flush=True)
        except (OSError, ValueError):
            pass

    def check(name: str, cond: bool, detail: str = "") -> None:
        nonlocal failed
        if not cond:
            failed += 1
        log(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"（{detail}）" if detail else ""))

    log(f"{APP_NAME} {APP_VERSION} 打包自检")
    log(f"  可执行文件：{sys.executable}")
    log(f"  frozen     ：{getattr(sys, 'frozen', False)}")
    log(f"  解包目录   ：{getattr(sys, '_MEIPASS', '（无，源码运行）')}")

    tmp = Path(tempfile.mkdtemp(prefix="ac-selftest-"))
    try:
        try:
            ffmpeg, ffprobe = find_tools()
            log(f"  ffmpeg     ：{ffmpeg}")
            log(f"  ffprobe    ：{ffprobe}")
            check("随包 ffmpeg 已定位", True)
            check("ffprobe 与 ffmpeg 同目录", ffmpeg.parent == ffprobe.parent)
            log(f"  版本       ：{version_text()}")
            enc = available_encoders()
            check("5 个输出格式的编码器都在", len(enc) == len(TARGETS),
                  "、".join(f"{k}:{v}" for k, v in sorted(enc.items())))
        except FFmpegMissing as exc:
            check("随包 ffmpeg 已定位", False, str(exc))
            return _write_selftest_log(lines, out, failed)

        # 现场合成素材：5 秒 440Hz 正弦波，192k mp3
        sample = tmp / "selftest_5s.mp3"
        rc, err = run_ffmpeg([
            "-f", "lavfi", "-i", "sine=frequency=440:duration=5",
            "-c:a", "libmp3lame", "-b:a", "192k", "-y", str(sample)])
        check("包内 ffmpeg 能编码（libmp3lame 可用）",
              rc == 0 and sample.is_file(),
              f"{sample.stat().st_size / 1024:.0f}KB" if sample.is_file() else err[-120:])
        if rc != 0 or not sample.is_file():
            return _write_selftest_log(lines, out, failed)

        info = probe_audio(sample)
        check("包内 ffprobe 能读出规格",
              info.duration > 4.5 and info.codec == "mp3",
              f"{info.duration:.1f}s / {info.codec} / {info.bitrate:.0f}kbps")

        before = hashlib.sha256(sample.read_bytes()).hexdigest()
        with NetworkAudit() as audit:  # 打包后的 exe 也照样不许外连
            wav = convert_one(sample, "wav", "keep", out_dir=tmp / "out")
            again = convert_one(sample, "mp3", "keep", out_dir=tmp / "out")
        check("转换过程 0 次联网", audit.attempts == 0, audit.summary())
        check("转 wav 成功出片", bool(wav.dst) and Path(wav.dst).is_file(),
              wav.note or "")
        if wav.dst and Path(wav.dst).is_file():
            check("输出落在独立 out 目录", Path(wav.dst).parent.name == "out")
            out_info = probe_audio(Path(wav.dst))
            check("产物可解析且时长对得上",
                  abs(out_info.duration - info.duration) < 0.15,
                  f"{out_info.duration:.2f}s / {out_info.codec}")
        check("同码率 mp3 → mp3 走直通（未重新编码）",
              again.ok and again.passthrough and not again.reencoded,
              again.note or "")
        check("原文件字节未被改写",
              hashlib.sha256(sample.read_bytes()).hexdigest() == before)
    except Exception as exc:  # 自检本身不该崩在异常上，崩了也要留下日志
        check("自检过程未抛异常", False, f"{type(exc).__name__}: {exc}")
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return _write_selftest_log(lines, out, failed)


def _write_selftest_log(lines: list[str], out: Path | None, failed: int) -> int:
    import tempfile
    log_path = out or (Path(tempfile.gettempdir()) / "音频格式转换工具-自检日志.txt")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    head = "结论：全部通过" if failed == 0 else f"结论：{failed} 项失败"
    text = "\n".join(lines + ["", "=" * 40, head, ""])
    log_path.write_text(text, encoding="utf-8")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    if "--cli" in sys.argv:
        from cli import main as cli_main
        sys.exit(cli_main([a for a in sys.argv[1:] if a != "--cli"]))

    if "--selftest" in sys.argv:
        # 打包验收用：不建窗口、不弹任何对话框，结果写日志文件后退出
        idx = sys.argv.index("--selftest")
        target = Path(sys.argv[idx + 1]) if len(sys.argv) > idx + 1 else None
        sys.exit(run_selftest(target))

    set_windows_app_id()
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(COMPANY)
    app.setWindowIcon(app_icon())

    configure_app(app)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())
