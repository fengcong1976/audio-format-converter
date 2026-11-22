"""界面自检：offscreen 跑，不需要人看，也不用真的弹窗。

    python tools/ui_test.py

覆盖：拖入 → 表格填充 → 参数联动（flac 置灰码率）→ 真转 → 结果表 →
双击详情字段 → 直通标注 → 取消 → 转换中关窗口 → 缺 ffmpeg 置灰 →
关于 / 使用说明的内容。全部断言通过才算界面可用。

用 offscreen 而不是真窗口：CI 和远程桌面里也能跑；这里只验证控件状态与
信号接线，不验证字体与像素（截图交给 tools/make_docs_shots.py）。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QMessageBox, QTableWidget,
)

import main as m  # noqa: E402
from core.about import APP_VERSION, COMPANY, USAGE_SHORT, WEBSITE  # noqa: E402
from core.ffmpeg_tool import FFmpegMissing, find_tools, run_ffmpeg  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
APP: QApplication | None = None


def check(name: str, cond: bool, detail: str = "") -> bool:
    RESULTS.append((name, bool(cond), detail))
    print(f"  {'PASS' if cond else 'FAIL'}  {name}"
          + (f"（{detail}）" if detail else ""), flush=True)
    return bool(cond)


def pump(ms: int = 60) -> None:
    """转一会儿事件循环：QThread 的信号要靠这个才送得到主线程。"""
    end = time.time() + ms / 1000
    while time.time() < end:
        APP.processEvents()
        time.sleep(0.005)


def wait_until(cond, timeout: float = 60.0) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        APP.processEvents()
        if cond():
            APP.processEvents()
            return True
        time.sleep(0.01)
    return bool(cond())


def make_samples(base: Path):
    ffmpeg, _ = find_tools()

    def gen(path: Path, seconds: float, codec: list[str]):
        run_ffmpeg(["-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
                    *codec, "-y", str(path)])

    short = base / "短音频_8s.mp3"
    short2 = base / "另一段_5s.flac"
    long = base / "长音频_600s.wav"
    gen(short, 8, ["-c:a", "libmp3lame", "-b:a", "192k"])
    gen(short2, 5, ["-c:a", "flac"])
    gen(long, 600, ["-c:a", "pcm_s16le"])
    return short, short2, long


def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def new_window(width: int = 1120, height: int = 720) -> "m.MainWindow":
    w = m.MainWindow()
    w.resize(width, height)
    w.show()
    pump(120)
    return w


def run_conversion(w, timeout: float = 120.0) -> bool:
    w.start()
    ok = wait_until(lambda: w.worker is None, timeout)
    pump(100)
    return ok


def main() -> int:
    global APP
    base = Path(tempfile.mkdtemp(prefix="ac-ui-"))
    short, short2, long_wav = make_samples(base)
    APP = QApplication(sys.argv)
    m.configure_app(APP)

    # 弹窗一律接住并记录，不用人点
    said: dict[str, object] = {}
    QMessageBox.information = staticmethod(
        lambda *a, **k: said.setdefault("info", a[2] if len(a) > 2 else ""))
    QMessageBox.warning = staticmethod(
        lambda *a, **k: said.setdefault("warn", a[2] if len(a) > 2 else ""))
    QMessageBox.critical = staticmethod(
        lambda *a, **k: said.setdefault("critical", a[2] if len(a) > 2 else ""))
    QMessageBox.question = staticmethod(
        lambda *a, **k: QMessageBox.StandardButton.Yes)
    opened: list[str] = []
    m.QDesktopServices.openUrl = staticmethod(
        lambda url: (opened.append(url.toString()), True)[1])

    w = new_window()
    try:
        print("\n一、空态与默认值", flush=True)
        check("01 窗口标题含产品名", m.APP_NAME in w.windowTitle(), w.windowTitle())
        check("02 初始表格为空", w.table.rowCount() == 0)
        check("03 初始「开始转换」可用（ffmpeg 正常）", w.start_btn.isEnabled())
        check("04 初始进度 0 且文案为「尚未开始」",
              w.progress.value() == 0 and "尚未开始" in w.progress.format())
        check("05 初始底部状态写明本地处理", "本地处理" in w.net_label.text(),
              w.net_label.text())
        check("06 默认输出格式 mp3", w.current_format() == "mp3", w.current_format())
        check("07 默认码率「保持原码率」", w.current_bitrate() == "keep")
        check("08 默认音量 100%，标签同步", w.gain.value() == 100
              and w.gain_label.text() == "100%")
        check("09 默认淡入淡出 0 秒", w.fade.value() == 0)
        check("10 拖拽区默认提示可拖文件", "拖到这里" in w.drop_label.text())
        check("11 底部「关于 / 使用说明」按钮存在",
              any(b.text() == "关于" for b in w.findChildren(m.QPushButton))
              and any(b.text() == "使用说明" for b in w.findChildren(m.QPushButton)))

        print("\n二、加入列表与预读", flush=True)
        w.paths = [str(short), str(short2)]
        files = w.preview_selection()
        check("12 拖入两个文件后表格 2 行", w.table.rowCount() == 2 and len(files) == 2)
        check("13 文件名列显示真实文件名", w.table.item(0, 0).text() == short.name)
        check("14 格式列显示源格式", w.table.item(0, 1).text() == "mp3")
        check("15 大小列不是空的", w.table.item(0, 4).text() not in ("", "—"))
        check("16 初始状态为「待转换」", w.table.item(0, 5).text() == "待转换")
        check("17 拖拽区提示待转换数量", "待转换" in w.drop_label.text(),
              w.drop_label.text())
        wait_until(lambda: w.prober is None or not w.prober.isRunning(), 20)
        pump(150)
        check("18 后台预读回填时长列（mm:ss）", ":" in w.table.item(0, 3).text(),
              w.table.item(0, 3).text())
        check("19 后台预读回填码率列", "kbps" in w.table.item(0, 2).text(),
              w.table.item(0, 2).text())

        print("\n三、列表操作", flush=True)
        w.table.selectRow(0)
        pump(60)
        check("20 选中一行后「删除选中」可用", w.delete_btn.isEnabled())
        w.delete_selected()
        pump(60)
        check("21 删除选中后只剩 1 行", w.table.rowCount() == 1, str(w.table.rowCount()))
        check("22 删除提示写明不会删文件", "不会删除" in w.net_label.text()
              or "原文件未改动" in w.net_label.text(), w.net_label.text())
        check("23 磁盘上的原文件仍在", short.exists() and short2.exists())
        w.clear_list()
        pump(60)
        check("24 清空列表后表格为 0 行", w.table.rowCount() == 0)
        check("25 清空后原文件仍在（只清清单）",
              short.exists() and short2.exists() and "原文件未改动" in w.net_label.text())

        print("\n四、参数联动", flush=True)
        idx = w.fmt.findData("flac")
        w.fmt.setCurrentIndex(idx)
        pump(60)
        check("26 选 flac 后码率下拉置灰", not w.bitrate.isEnabled())
        check("27 无损提示写明码率不生效",
              "无损格式，码率不生效" in w.bitrate_hint.text(), w.bitrate_hint.text())
        w.fmt.setCurrentIndex(w.fmt.findData("mp3"))
        pump(60)
        check("28 切回 mp3 后码率下拉恢复", w.bitrate.isEnabled()
              and w.bitrate_hint.text() == "")
        w.gain.setValue(150)
        pump(60)
        check("29 音量拖到 150% 标签同步", w.gain_label.text() == "150%")
        w.gain.setValue(0)
        check("30 音量 0% 标签同步", w.gain_label.text() == "0%")
        w.gain.setValue(100)
        check("31 剪切输入上限为 24 小时",
              w.start_spin.maximum() == 86400 and w.end_spin.maximum() == 86400)
        said.clear()
        w.paths = [str(short)]
        w.preview_selection()
        w.start_spin.setValue(5)
        w.end_spin.setValue(3)
        w.start()
        pump(80)
        check("32 起始 ≥ 结束：弹提示且不启动转换",
              w.worker is None and "warn" in said, str(said.get("warn", ""))[:60])

        print("\n五、真实转换与结果表", flush=True)
        w.start_spin.setValue(0)
        w.end_spin.setValue(0)
        w.fmt.setCurrentIndex(w.fmt.findData("mp3"))
        w.bitrate.setCurrentIndex(w.bitrate.findData("128k"))  # 明示 128k → 必须重编码
        before = sha(short)
        ok = run_conversion(w)
        check("33 单条转换跑完并回到空闲", ok and w.worker is None)
        check("34 完成后「开始」可用、「取消」置灰",
              w.start_btn.isEnabled() and not w.cancel_btn.isEnabled())
        check("35 结果行状态为「完成」", w.table.item(0, 5).text() == "完成",
              w.table.item(0, 5).text())
        check("36 格式列写成「mp3 → mp3」", w.table.item(0, 1).text() == "mp3 → mp3",
              w.table.item(0, 1).text())
        check("37 码率列显示前后码率与单位",
              "→" in w.table.item(0, 2).text() and "kbps" in w.table.item(0, 2).text(),
              w.table.item(0, 2).text())
        check("38 大小列显示体积变化", "→" in w.table.item(0, 4).text(),
              w.table.item(0, 4).text())
        item0 = w.table.item(0, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        check("39 产物真实存在", item0 is not None and Path(item0.dst).is_file(),
              item0.dst if item0 else "无")
        check("40 报告 report.json 已写出",
              (Path(item0.dst).parent / "report.json").is_file())
        check("41 进度到 100% 且文案为完成",
              w.progress.value() == 100 and "完成" in w.progress.format(),
              w.progress.format())
        check("42 底部状态显示完成数与 0 次联网",
              "完成 1" in w.net_label.text() and "0 次" in w.net_label.text(),
              w.net_label.text())
        check("43 原文件哈希不变", sha(short) == before)

        print("\n六、直通标注与详情", flush=True)
        w.bitrate.setCurrentIndex(w.bitrate.findData("keep"))
        w.paths = [str(short)]
        w.preview_selection()
        ok = run_conversion(w)
        item1 = w.table.item(0, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        check("44 同码率 mp3 → mp3 标注「完成（直通）」",
              ok and w.table.item(0, 5).text() == "完成（直通）",
              w.table.item(0, 5).text())
        dlg = m.DetailDialog(item1, w)
        dlg.show()
        pump(100)
        detail_table = dlg.findChild(QTableWidget)
        rows = detail_table.rowCount() if detail_table else 0
        texts = [detail_table.item(r, 0).text() for r in range(rows)] if detail_table else []
        check("45 详情对话框字段 ≥ 10 行", rows >= 10, str(rows))
        for key in ("是否重新编码", "格式", "码率", "采样率", "声道",
                    "时长", "淡入淡出", "有损说明", "原文件是否改动"):
            if key not in texts:
                check(f"46 详情包含「{key}」", False, str(texts))
                break
        else:
            check("46 详情包含 9 个必备字段", True)
        head_value = ""
        if detail_table and rows:
            head_value = detail_table.item(0, 1).text()
        check("47 直通时详情写明「否（原编码直通复制）」",
              head_value == "否（原编码直通复制）", head_value)
        dlg.close()

        print("\n七、关于与使用说明", flush=True)
        about = m.AboutDialog(w)
        about.show()
        pump(80)
        about_text = " ".join(x.text() for x in about.findChildren(QLabel))
        check("48 关于框含版本号", APP_VERSION in about_text)
        check("49 关于框含公司全称", COMPANY in about_text)
        check("50 关于框含官网网址", WEBSITE in about_text)
        about.close()
        usage = m.UsageDialog(w)
        usage.show()
        pump(80)
        usage_text = " ".join(x.text() for x in usage.findChildren(QLabel))
        check("51 使用说明含「怎么用」与全部用法条目",
              "怎么用" in usage_text
              and all(u.split("；")[0][:8] in usage_text for u in USAGE_SHORT))
        usage.close()

        print("\n八、取消与关窗口", flush=True)
        w.paths = [str(long_wav)]
        w.preview_selection()
        w.start()
        wait_until(lambda: w.worker is not None, 5)
        pump(300)
        running = w.worker is not None
        check("52 转换中「开始」置灰、「取消」亮起、拖拽关闭",
              running and not w.start_btn.isEnabled() and w.cancel_btn.isEnabled()
              and not w.acceptDrops(),
              f"running={running}")
        check("53 转换中参数控件禁用",
              not w.fmt.isEnabled() and not w.gain.isEnabled()
              and not w.start_spin.isEnabled() and not w.limit_size.isEnabled()
              and not w.size_limit.isEnabled()
              and not w.btn_files.isEnabled() and not w.btn_dir.isEnabled())
        w.cancel()
        wait_until(lambda: w.worker is None, 30)
        pump(100)
        rep = w.report
        out_dir = long_wav.parent / "out"
        leftovers = list(out_dir.glob("*.flac")) if out_dir.is_dir() else []
        check("54 取消后回到空闲且报告标记 cancelled",
              rep is not None and rep.cancelled and w.start_btn.isEnabled(),
              f"cancelled={getattr(rep, 'cancelled', None)}")
        check("55 取消后没有半截产物", not leftovers, f"{len(leftovers)} 个残留")
        w.paths = []
        w.clear_list()
        pump(60)

        print("\n九、缺 ffmpeg 的降级", flush=True)
        original = m.available_encoders

        def boom():
            raise FFmpegMissing("找不到 ffmpeg（界面自检模拟）")
        m.available_encoders = boom
        try:
            broken_win = new_window()
            check("56 缺 ffmpeg 时「开始」置灰", not broken_win.start_btn.isEnabled())
            check("57 缺 ffmpeg 时拖拽区给出红字说明",
                  "ffmpeg" in broken_win.drop_label.text()
                  and "#b3411c" in broken_win.drop_label.styleSheet())
            broken_win.close()
        finally:
            m.available_encoders = original

        print("\n十、转换中关窗口", flush=True)
        w2 = new_window()
        w2.paths = [str(long_wav)]
        w2.preview_selection()
        w2.start()
        wait_until(lambda: w2.worker is not None, 5)
        pump(300)
        w2.close()
        closed = not w2.isVisible()
        stopped = wait_until(lambda: w2.worker is None or not w2.worker.isRunning(), 10)
        check("58 转换中关窗口：确认后窗口关闭且后台停下", closed and stopped,
              f"closed={closed} / stopped={stopped}")

        print("\n十一、限制目标大小", flush=True)
        w3 = new_window()
        w3.limit_size.setChecked(True)
        pump(60)
        check("59 勾选「限制大小」后码率下拉置灰、目标输入可用、提示写明自动选码率",
              not w3.bitrate.isEnabled() and w3.size_limit.isEnabled()
              and "自动选码率" in w3.bitrate_hint.text(),
              w3.bitrate_hint.text())
        w3.fmt.setCurrentIndex(w3.fmt.findData("flac"))
        pump(60)
        check("60 切到无损格式：限制大小自动取消并置灰",
              not w3.limit_size.isChecked() and not w3.limit_size.isEnabled())
        w3.fmt.setCurrentIndex(w3.fmt.findData("mp3"))
        w3.limit_size.setChecked(True)
        w3.size_limit.setValue(0.1)
        w3.paths = [str(short)]
        w3.preview_selection()
        pump(60)
        ok3 = run_conversion(w3)
        item3 = w3.table.item(0, 0).data(m.Qt.ItemDataRole.UserRole + 1)
        limit = int(round(0.1 * 1048576))
        check("61 按 0.1MB 目标转换：产物不超过目标、报告写明目标大小",
              ok3 and item3 is not None and item3.ok
              and 0 < item3.dst_bytes <= limit and item3.target_bytes == limit,
              f"{item3.dst_bytes if item3 else 0} / {limit} 字节")
        check("62 说明列标出目标体积", "目标" in w3.table.item(0, 6).text(),
              w3.table.item(0, 6).text())
        w3.close()

        print("\n" + "=" * 66, flush=True)
        passed = sum(1 for _, ok_, _ in RESULTS if ok_)
        total = len(RESULTS)
        if passed == total:
            print(f"界面自检通过：{passed}/{total} 项", flush=True)
            return 0
        print(f"界面自检失败：{passed}/{total}，以下未通过：", flush=True)
        for name, ok_, detail in RESULTS:
            if not ok_:
                print(f"  FAIL  {name}  {detail}", flush=True)
        return 1
    finally:
        try:
            w.close()
        except Exception:
            pass
        shutil.rmtree(base, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
