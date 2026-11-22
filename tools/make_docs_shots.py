"""生成文档用截图 docs/screenshots/*.png（必须用真实平台跑，别加 offscreen）。

offscreen 平台没有中文字体，抓出来全是方块，所以这里不设 QT_QPA_PLATFORM。
样本音频现场用 ffmpeg 的 lavfi 合成，不读用户文件、不联网：
四种常见来源（mp3 讲课录音 / wav 录音笔 / flac 无损音乐 / m4a 手机录音），
跑一遍真实转换后截图，保证截图里的数字都是程序当时真算出来的。

跑法：python tools/make_docs_shots.py
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import main as m  # noqa: E402
from core.ffmpeg_tool import find_tools  # noqa: E402
from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QMessageBox  # noqa: E402

OUT = ROOT / "docs" / "shots"


def build_samples(base: Path) -> Path:
    d = base / "示例音频"
    d.mkdir(parents=True, exist_ok=True)
    ffmpeg, _ = find_tools()

    def run(args):
        subprocess.run([str(ffmpeg), "-y", "-loglevel", "error", *args], check=True)

    def make(name, secs, codec_args, freq=440):
        run(["-f", "lavfi", "-i", f"sine=frequency={freq}:duration={secs}"]
            + codec_args + [str(d / name)])

    # 真实工作里最常见的一批：讲课录音（mp3）、录音笔（wav）、无损音乐（flac）、手机录音（m4a）
    make("课堂录音-第03讲.mp3", 45, ["-c:a", "libmp3lame", "-b:a", "128k"], freq=330)
    make("采访录音-录音笔.wav", 8, ["-c:a", "pcm_s16le"], freq=520)
    make("轻音乐-无损.flac", 12, ["-c:a", "flac"], freq=660)
    make("手机录音-会议.m4a", 10, ["-c:a", "aac", "-b:a", "128k"], freq=880)
    return d


def shot(widget, name: str) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    widget.grab().save(str(OUT / name))


def wait(w, ms: int) -> None:
    loop = QEventLoop()
    QTimer.singleShot(ms, loop.quit)
    loop.exec()


def main() -> int:
    base = Path(tempfile.mkdtemp(prefix="docs_ashots_"))
    print("生成样本…")
    samples = build_samples(base)
    for p in sorted(samples.iterdir()):
        print(f"  {p.name}: {p.stat().st_size / 1024:.0f} KB")

    app = QApplication(sys.argv)
    m.configure_app(app)
    # 弹窗一律吞掉：截图时不需要它们挡住窗口
    QMessageBox.information = staticmethod(lambda *a, **k: None)
    QMessageBox.warning = staticmethod(lambda *a, **k: None)
    QMessageBox.critical = staticmethod(lambda *a, **k: None)
    QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
    m.QDesktopServices.openUrl = staticmethod(lambda *a, **k: True)

    print("渲染窗口…")
    empty = m.MainWindow()
    empty.show()
    wait(empty, 500)
    shot(empty, "00-空白窗口.png")
    empty.close()

    w = m.MainWindow()
    w.resize(1180, 760)
    w.show()
    w.paths = [str(samples)]
    w.preview_selection()
    # 等后台把时长 / 码率读完，截图里才不是一排「—」
    for _ in range(40):
        if w.prober is None or not w.prober.isRunning():
            break
        wait(w, 100)
    wait(w, 200)
    shot(w, "01-加入列表.png")

    # 切到无损格式：码率行会置灰并给出提示，截一张说明「无损没有码率」
    idx = w.fmt.findData("flac")
    if idx >= 0:
        w.fmt.setCurrentIndex(idx)
    wait(w, 250)
    shot(w, "02-无损格式提示.png")
    idx = w.fmt.findData("mp3")
    if idx >= 0:
        w.fmt.setCurrentIndex(idx)
    # 截图用默认的「保持原码率」：这是程序推荐的选项，也不会把文件转胖
    bidx = w.bitrate.findData("keep")
    if bidx >= 0:
        w.bitrate.setCurrentIndex(bidx)
    wait(w, 150)

    t0 = time.perf_counter()
    w.start()
    loop = QEventLoop()
    if w.worker is not None:
        w.worker.done.connect(lambda *a: loop.quit())
    QTimer.singleShot(600000, loop.quit)
    loop.exec()
    print(f"转换完成，耗时 {time.perf_counter() - t0:.1f}s")
    wait(w, 400)
    for r in range(w.table.rowCount()):
        print("   ", " | ".join(w.table.item(r, c).text()[:40] for c in (0, 1, 3, 6)))
    shot(w, "03-转换结果.png")

    # 多选两行，展示「删除选中」按钮亮起
    w.table.selectRow(0)
    sm = w.table.selectionModel()
    sm.select(w.table.model().index(2, 0),
              sm.SelectionFlag.Select | sm.SelectionFlag.Rows)
    wait(w, 200)
    shot(w, "04-删除选中.png")

    # 详情页：取第一条有损重编码的结果
    for r in range(w.table.rowCount()):
        it = w.table.item(r, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        if it is not None and it.ok and not it.passthrough:
            dlg = m.DetailDialog(it, w)
            dlg.show()
            wait(dlg, 400)
            shot(dlg, "05-详情.png")
            dlg.close()
            break

    u = m.UsageDialog(w)
    u.show()
    wait(u, 400)
    shot(u, "06-使用说明.png")
    u.close()

    a = m.AboutDialog(w)
    a.show()
    wait(a, 400)
    shot(a, "07-关于.png")
    a.close()
    w.close()

    print("截图已写入：", OUT)
    return 0


if __name__ == "__main__":
    sys.exit(main())
