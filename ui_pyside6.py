#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写真提示词抽卡台 v1 —— PySide6 版。

采样（sampler.py）+ 出图（agnes.py）的桌面界面，QSS 美化。

用法：
    python ui_pyside6.py            打开图形界面
    python ui_pyside6.py --selftest 不开界面，验证输出解析逻辑
    python ui_pyside6.py --smoke    构建窗口后立即销毁（自动化冒烟）

依赖：pip install PySide6
原 tkinter 版见 ui.py，本文件与之功能对齐、可独立运行。
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import random
import re
import subprocess
import sys
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QUrl, QSettings
from PySide6.QtGui import QAction, QClipboard, QColor, QDesktopServices, QFont, QTextCharFormat
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFrame, QGridLayout,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
    QMainWindow, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QSplitter, QStatusBar, QTableWidget, QTableWidgetItem,
    QTextEdit, QVBoxLayout, QWidget,
)

import agnes
import sampler

# ---------------------------------------------------------------------------
# 路径与常量（与 ui.py 保持一致，单一事实源在 sampler.py / agnes.py）
# ---------------------------------------------------------------------------
FROZEN = bool(getattr(sys, "frozen", False))
PROJECT_ROOT = sampler.PROJECT_ROOT
SAMPLER = PROJECT_ROOT / "sampler.py"
GEN = PROJECT_ROOT / "agnes.py"
SPEC = sampler.spec_path()
PROMPTS = agnes.queue_path()
OUTDIR = agnes.outdir_path()
RUNLOG = agnes.runlog_path()
MANIFEST = sampler.project_path(
    sampler.env_path("GACHA_MANIFEST", sampler.DEFAULT_MANIFEST))

SLOT_KEYS = ["hair", "gaze", "anchor", "scene", "outfit", "pose", "light"]
PACK_ANY = "全部(轮转)"
PACKS = [PACK_ANY, "city", "home", "night", "retro", "cafe", "athleisure"]
QUALITIES = {"medium（抽卡→1K）": "medium", "high（定稿→2K）": "high", "默认": None}
TIERS = {"自动（按质量映射）": None, "1K": "1K", "2K": "2K", "3K": "3K", "4K": "4K"}
RATIO_OPTS = {"自动（词库锁定 3:4）": None}
RATIO_OPTS.update({r: r for r in agnes.RATIOS})

RE_SEED = re.compile(r"^draw_seed=(\d+)\s+产出 (\d+) 条$")
RE_FAIL = re.compile(r"^第 (\d+) 次抽卡失败：(.*)$")
RE_ROW = re.compile(r"^\s{2}(\S+)\s+(\d+)字\s+(\w+) / (.+?)(?:\s+\[(.*)\])?$")
RE_GEN = re.compile(r"^UI\|(OK|FAIL|SKIP|DRY)\|([^|]+)\|(\d+)\|?(.*)$")
RE_BATCH_TOTAL = re.compile(r"^batch\s+(\d+) 条")


# ---------------------------------------------------------------------------
# 解析函数（与 ui.py 完全一致，selftest 复用）
# ---------------------------------------------------------------------------
def parse_output(stdout: str) -> dict:
    info = {"seed": None, "count": 0, "fail": None, "rows": []}
    for line in stdout.splitlines():
        m = RE_ROW.match(line)
        if m:
            info["rows"].append({
                "variant_id": m.group(1), "chars": int(m.group(2)),
                "pack": m.group(3), "outfit": m.group(4),
                "warn": m.group(5) or "",
            })
            continue
        line = line.strip()
        m = RE_SEED.match(line)
        if m:
            info["seed"] = m.group(1)
            info["count"] = int(m.group(2))
            continue
        m = RE_FAIL.match(line)
        if m:
            info["fail"] = m.group(2)
    return info


def parse_gen_line(line: str):
    line = line.strip()
    if not line.startswith("UI|"):
        return None
    m = RE_GEN.match(line)
    if not m:
        parts = line.split("|")
        return {"state": parts[1] if len(parts) > 1 else "?",
                "name": parts[2] if len(parts) > 2 else "", "bytes": 0, "detail": ""}
    return {"state": m.group(1), "name": m.group(2),
            "bytes": int(m.group(3)), "detail": m.group(4).strip()}


def load_records(path: Path) -> dict:
    recs = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                recs[rec["variant_id"]] = rec
    return recs


def run_sampler(extra_args):
    """源码模式开子进程；EXE 模式同进程调用 sampler.main 并重定向输出。"""
    if not FROZEN:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(SAMPLER)] + extra_args,
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(PROJECT_ROOT), env=env)
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sampler.main(extra_args)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001
        err.write(f"{type(exc).__name__}: {exc}\n")
        code = 1
    return subprocess.CompletedProcess(extra_args, code, out.getvalue(), err.getvalue())


def gen_args(tier=None, ratio=None, workers=None, overwrite=False):
    args = []
    if tier:
        args += ["--size", tier]
    if ratio:
        args += ["--ratio", ratio]
    if workers and workers > 1:
        args += ["--workers", str(workers)]
    if overwrite:
        args += ["--overwrite"]
    return args


# ---------------------------------------------------------------------------
# 工作线程
# ---------------------------------------------------------------------------
class DrawWorker(QThread):
    """抽卡线程：避免阻塞 UI。"""
    finished_ok = Signal(object)  # CompletedProcess

    def __init__(self, args):
        super().__init__()
        self.args = args

    def run(self):
        proc = run_sampler(self.args)
        self.finished_ok.emit(proc)


class _EmitterSink(io.TextIOBase):
    """把 print() 按行 emit 成信号，EXE 同进程模式下替代子进程 stdout 管道。"""

    def __init__(self, line_signal: Signal):
        self.sig = line_signal
        self.buf = ""

    def write(self, text: str):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.sig.emit(line.rstrip("\r\n"))
        return len(text)

    def flush(self):
        if self.buf.strip():
            self.sig.emit(self.buf.strip())
        self.buf = ""


class GenWorker(QThread):
    """出图线程：子进程逐行泵输出，或 EXE 同进程重定向。"""
    line = Signal(str)
    done = Signal(int)

    def __init__(self, extra_args, inprocess=False):
        super().__init__()
        self.extra_args = extra_args
        self.inprocess = inprocess
        self.proc = None
        self._stop = False

    def run(self):
        if self.inprocess:
            agnes.ABORT = False
            sink = _EmitterSink(self.line)
            code = 1
            try:
                with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                    code = agnes.main(self.extra_args) or 0
            except SystemExit as exc:
                code = exc.code if isinstance(exc.code, int) else 1
            except Exception as exc:  # noqa: BLE001
                sink.write(f"[error] {type(exc).__name__}: {exc}\n")
            finally:
                sink.flush()
            self.done.emit(code)
            return

        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            self.proc = subprocess.Popen(
                [sys.executable, str(GEN)] + self.extra_args,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", bufsize=1,
                cwd=str(PROJECT_ROOT), env=env)
        except OSError as exc:
            self.line.emit(f"[启动失败] {exc}")
            self.done.emit(1)
            return
        try:
            for line in self.proc.stdout:
                if self._stop:
                    break
                self.line.emit(line.rstrip("\n"))
        except (OSError, ValueError):
            pass
        code = self.proc.wait()
        self.done.emit(code)

    def stop(self):
        self._stop = True
        if self.inprocess:
            agnes.ABORT = True
        elif self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 槽位覆盖行（动态添加/删除）
# ---------------------------------------------------------------------------
class OverrideRow(QWidget):
    removed = Signal(QWidget)

    def __init__(self, key="", value="", parent=None):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self.key_combo = QComboBox()
        self.key_combo.setEditable(True)
        self.key_combo.addItems(SLOT_KEYS)
        self.key_combo.setCurrentText(key)
        self.key_combo.setMinimumWidth(100)
        self.value_edit = QLineEdit(value)
        self.value_edit.setPlaceholderText("词库文案或任意文本")
        self.del_btn = QPushButton("✕")
        self.del_btn.setFixedWidth(32)
        self.del_btn.clicked.connect(lambda: self.removed.emit(self))
        lay.addWidget(self.key_combo)
        lay.addWidget(self.value_edit, 1)
        lay.addWidget(self.del_btn)

    def data(self):
        return self.key_combo.currentText().strip(), self.value_edit.text().strip()


# ---------------------------------------------------------------------------
# 主窗口
# ---------------------------------------------------------------------------
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.records = load_records(MANIFEST)
        self.override_rows: list[OverrideRow] = []
        self.gen_worker: GenWorker | None = None
        self.draw_worker: DrawWorker | None = None
        self.gen_done = 0
        self.gen_total = 0
        self.settings = QSettings("GeneratePrompt", "GachaUI")
        self.theme = self.settings.value("theme", "light", type=str)

        self.setWindowTitle("写真提示词抽卡台 v1 · Agnes 出图（PySide6）")
        self.resize(1240, 820)
        self.setMinimumSize(1060, 700)

        central = QWidget()
        self.setCentralWidget(central)
        root = QHBoxLayout(central)
        root.setContentsMargins(12, 12, 12, 8)
        root.setSpacing(12)

        root.addWidget(self._build_left(), 0)
        root.addWidget(self._build_right(), 1)

        self._build_status()
        self.refresh_key_state()
        self.apply_theme(self.theme)

    # ---------------- 左侧参数面板 ----------------
    def _build_left(self) -> QWidget:
        outer = QFrame()
        outer.setObjectName("leftPanel")
        outer.setFixedWidth(340)
        lay = QVBoxLayout(outer)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content = QWidget()
        cl = QVBoxLayout(content)
        cl.setContentsMargins(12, 12, 12, 12)
        cl.setSpacing(10)

        title = QLabel("抽卡参数")
        title.setObjectName("panelTitle")
        cl.addWidget(title)

        # ---- 采样分组 ----
        sample = QGroupBox(" 采样 ")
        sg = QGridLayout(sample)
        sg.setVerticalSpacing(6)
        sg.setHorizontalSpacing(8)

        sg.addWidget(QLabel("风格包"), 0, 0)
        self.pack_combo = QComboBox()
        self.pack_combo.addItems(PACKS)
        sg.addWidget(self.pack_combo, 1, 0, 1, 2)

        sg.addWidget(QLabel("条数（1-20）"), 2, 0)
        self.n_spin = QSpinBox()
        self.n_spin.setRange(1, 20)
        self.n_spin.setValue(5)
        sg.addWidget(self.n_spin, 3, 0, 1, 2)

        sg.addWidget(QLabel("draw_seed（留空=随机）"), 4, 0)
        self.seed_edit = QLineEdit()
        self.seed_edit.setPlaceholderText("整数，留空随机")
        sg.addWidget(self.seed_edit, 5, 0, 1, 2)

        sg.addWidget(QLabel("质量档"), 6, 0)
        self.quality_combo = QComboBox()
        self.quality_combo.addItems(list(QUALITIES))
        sg.addWidget(self.quality_combo, 7, 0, 1, 2)

        sg.addWidget(QLabel("槽位覆盖（值可填词库文案或任意文本）"), 8, 0, 1, 2)
        self.override_container = QWidget()
        self.override_lay = QVBoxLayout(self.override_container)
        self.override_lay.setContentsMargins(0, 0, 0, 0)
        self.override_lay.setSpacing(4)
        sg.addWidget(self.override_container, 9, 0, 1, 2)
        self.add_override_row()

        add_btn = QPushButton("+ 添加覆盖行")
        add_btn.clicked.connect(self.add_override_row)
        sg.addWidget(add_btn, 10, 0)

        cl.addWidget(sample)

        self.btn_draw = QPushButton("开始抽卡")
        self.btn_draw.setObjectName("primaryButton")
        self.btn_draw.clicked.connect(self.run_draw)
        cl.addWidget(self.btn_draw)

        # ---- Agnes 出图分组 ----
        gen = QGroupBox(" Agnes Image 2.5 Flash 出图 ")
        gg = QGridLayout(gen)
        gg.setVerticalSpacing(6)
        gg.setHorizontalSpacing(8)

        gg.addWidget(QLabel("输出档位"), 0, 0, 1, 2)
        self.tier_combo = QComboBox()
        self.tier_combo.addItems(list(TIERS))
        gg.addWidget(self.tier_combo, 1, 0, 1, 2)

        gg.addWidget(QLabel("宽高比"), 2, 0, 1, 2)
        self.ratio_combo = QComboBox()
        self.ratio_combo.addItems(list(RATIO_OPTS))
        gg.addWidget(self.ratio_combo, 3, 0, 1, 2)

        row = QHBoxLayout()
        row.addWidget(QLabel("并发"))
        self.workers_spin = QSpinBox()
        self.workers_spin.setRange(1, 8)
        self.workers_spin.setValue(2)
        row.addWidget(self.workers_spin)
        self.overwrite_check = QCheckBox("覆盖已出图片")
        row.addWidget(self.overwrite_check)
        row.addStretch()
        gg.addLayout(row, 4, 0, 1, 2)

        self.key_label = QLabel("密钥：检测中…")
        self.key_label.setWordWrap(True)
        self.key_label.setObjectName("keyLabel")
        gg.addWidget(self.key_label, 5, 0, 1, 2)

        keyrow = QHBoxLayout()
        btn_key = QPushButton("设置密钥")
        btn_key.clicked.connect(self.set_key)
        btn_recheck = QPushButton("重检")
        btn_recheck.clicked.connect(self.refresh_key_state)
        keyrow.addWidget(btn_key)
        keyrow.addWidget(btn_recheck)
        keyrow.addStretch()
        gg.addLayout(keyrow, 6, 0, 1, 2)

        self.btn_gen_all = QPushButton("出图：任务队列全部")
        self.btn_gen_all.setObjectName("primaryButton")
        self.btn_gen_all.clicked.connect(self.gen_queue_all)
        gg.addWidget(self.btn_gen_all, 7, 0, 1, 2)

        self.btn_gen_stop = QPushButton("停止出图")
        self.btn_gen_stop.setEnabled(False)
        self.btn_gen_stop.clicked.connect(self.stop_gen)
        gg.addWidget(self.btn_gen_stop, 8, 0, 1, 2)

        cl.addWidget(gen)

        tip = QLabel("采样只写任务队列，出图由 agnes.py 消费；\n"
                     "重复组合会被 manifest 全量去重自动重抽。")
        tip.setObjectName("hintLabel")
        tip.setWordWrap(True)
        cl.addWidget(tip)
        cl.addStretch()

        scroll.setWidget(content)
        lay.addWidget(scroll)
        return outer

    def add_override_row(self, key="", value=""):
        row = OverrideRow(key, value)
        row.removed.connect(self.remove_override_row)
        self.override_lay.addWidget(row)
        self.override_rows.append(row)

    def remove_override_row(self, row: OverrideRow):
        if row in self.override_rows:
            self.override_rows.remove(row)
        row.setParent(None)
        row.deleteLater()

    # ---------------- 右侧：结果 + 日志 ----------------
    def _build_right(self) -> QWidget:
        splitter = QSplitter(Qt.Vertical)
        splitter.addWidget(self._build_results())
        splitter.addWidget(self._build_gen_log())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        return splitter

    def _build_results(self) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)

        bar = QHBoxLayout()
        title = QLabel("抽卡结果")
        title.setObjectName("panelTitle")
        bar.addWidget(title)
        bar.addStretch()
        self.btn_theme = QPushButton()
        self.btn_theme.setFixedWidth(110)
        self.btn_theme.clicked.connect(self.toggle_theme)
        bar.addWidget(self.btn_theme)
        btn_open_prompts = QPushButton("打开任务队列目录")
        btn_open_prompts.clicked.connect(self.open_prompts_dir)
        btn_open_out = QPushButton("打开出图目录")
        btn_open_out.clicked.connect(self.open_outdir)
        btn_refresh = QPushButton("刷新记录")
        btn_refresh.clicked.connect(self.refresh_records)
        bar.addWidget(btn_open_prompts)
        bar.addWidget(btn_open_out)
        bar.addWidget(btn_refresh)
        lay.addLayout(bar)

        cols = ["变体ID", "字数", "包", "服装", "出图", "状态"]
        self.table = QTableWidget(0, len(cols))
        self.table.setHorizontalHeaderLabels(cols)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setSelectionMode(QTableWidget.SingleSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setAlternatingRowColors(True)
        hdr = self.table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.Fixed)
        hdr.setSectionResizeMode(1, QHeaderView.Fixed)
        hdr.setSectionResizeMode(2, QHeaderView.Fixed)
        hdr.setSectionResizeMode(3, QHeaderView.Stretch)
        hdr.setSectionResizeMode(4, QHeaderView.Fixed)
        hdr.setSectionResizeMode(5, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 200)
        self.table.setColumnWidth(1, 56)
        self.table.setColumnWidth(2, 80)
        self.table.setColumnWidth(4, 70)
        self.table.itemSelectionChanged.connect(self.on_select)
        lay.addWidget(self.table, 1)

        lay.addWidget(QLabel("提示词预览（点击上方条目）"))
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setMinimumHeight(120)
        lay.addWidget(self.preview, 1)

        btns = QHBoxLayout()
        self.btn_gen_one = QPushButton("出图（选中）")
        self.btn_gen_one.clicked.connect(self.gen_selected)
        btn_copy = QPushButton("复制提示词")
        btn_copy.clicked.connect(self.copy_prompt)
        self.btn_final = QPushButton("定稿入队(high)")
        self.btn_final.clicked.connect(self.finalize_high)
        self.queue_label = QLabel(f"任务队列：{sampler.display_path(PROMPTS)}")
        self.queue_label.setObjectName("hintLabel")
        btns.addWidget(self.btn_gen_one)
        btns.addWidget(btn_copy)
        btns.addWidget(self.btn_final)
        btns.addWidget(self.queue_label)
        btns.addStretch()
        lay.addLayout(btns)
        return pane

    def _build_gen_log(self) -> QWidget:
        pane = QWidget()
        lay = QVBoxLayout(pane)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)

        head = QHBoxLayout()
        head.addWidget(QLabel("出图日志"))
        self.gen_progress = QLabel("空闲")
        self.gen_progress.setObjectName("hintLabel")
        head.addWidget(self.gen_progress)
        head.addStretch()
        btn_open_img = QPushButton("打开图片目录")
        btn_open_img.clicked.connect(self.open_outdir)
        btn_clear = QPushButton("清空")
        btn_clear.clicked.connect(self._log_clear)
        head.addWidget(btn_open_img)
        head.addWidget(btn_clear)
        lay.addLayout(head)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setObjectName("logView")
        self.log.setFont(QFont("Consolas", 9))
        lay.addWidget(self.log, 1)
        return pane

    def _build_status(self):
        self.statusBar().showMessage(f"就绪。词库：{sampler.display_path(SPEC)}")

    # ---------------- 主题切换 ----------------
    def apply_theme(self, theme: str):
        self.theme = theme
        qss = LIGHT_QSS if theme == "light" else DARK_QSS
        QApplication.instance().setStyleSheet(qss)
        self.btn_theme.setText("🌙 深色模式" if theme == "light" else "☀️ 浅色模式")
        self.settings.setValue("theme", theme)

    def toggle_theme(self):
        self.apply_theme("dark" if self.theme == "light" else "light")

    # ---------------- 日志 ----------------
    def _log_clear(self):
        self.log.clear()

    def _log(self, text: str, color: str = "#d4d4d4"):
        fmt = QTextCharFormat()
        fmt.setForeground(QColor(color))
        self.log.mergeCurrentCharFormat(fmt)
        self.log.appendPlainText(text)
        self.log.moveCursor(QTextCursor.End)

    def _set_running(self, running: bool):
        state = not running
        for btn in (self.btn_draw, self.btn_gen_one, self.btn_gen_all, self.btn_final):
            btn.setEnabled(state)
        self.btn_gen_stop.setEnabled(running)

    # ---------------- 密钥 ----------------
    def refresh_key_state(self):
        key, source = agnes.resolve_api_key()
        if key:
            self.key_label.setText(f"密钥：{agnes.mask_key(key)}\n来源：{source}")
            self.key_label.setStyleSheet("color:#1a7f3b;")
        else:
            self.key_label.setText("密钥：未配置。点「设置密钥」，或写入项目根 agnes.key")
            self.key_label.setStyleSheet("color:#b91c1c;")
        self.statusBar().showMessage(f"密钥状态已刷新：{'已配置' if key else '未配置'}")

    def set_key(self):
        key, ok = QInputDialog.getText(
            self, "设置 Agnes API Key", "粘贴 API Key：",
            echo=QLineEdit.Password)
        if not ok or not key.strip():
            return
        target = agnes.set_api_key(key.strip())
        self.refresh_key_state()
        self._log(f"[key] 已写入 {sampler.display_path(target)}（已 gitignore）", "#9cdcfe")

    # ---------------- 出图 ----------------
    def _start_gen(self, extra_args: list, title: str) -> bool:
        if self.gen_worker is not None:
            QMessageBox.information(self, "出图", "已有出图任务在跑，先等它结束或点「停止出图」")
            return False
        key, _src = agnes.resolve_api_key()
        if not key:
            QMessageBox.critical(self, "缺少密钥", "先设置 API Key：左侧「Agnes 出图」→「设置密钥」")
            return False
        OUTDIR.mkdir(parents=True, exist_ok=True)

        inprocess = FROZEN
        self.gen_worker = GenWorker(extra_args, inprocess=inprocess)
        self.gen_worker.line.connect(self._handle_line)
        self.gen_worker.done.connect(self._finish_gen)

        self.gen_done = 0
        self.gen_total = 0
        self._log_clear()
        command = ("[EXE 同进程] agnes.main " if inprocess else "python agnes.py ") + " ".join(extra_args)
        self._log(f"$ {command}", "#9cdcfe")
        self._log(f"—— {title} ——", "#9cdcfe")
        self._set_running(True)
        self.gen_progress.setText(f"{title}：0/…")
        self.gen_worker.start()
        return True

    def _handle_line(self, line: str):
        if not line.strip():
            return
        total = RE_BATCH_TOTAL.match(line)
        if total:
            self.gen_total = int(total.group(1))
            self._refresh_progress()
        parsed = parse_gen_line(line)
        if parsed:
            self.gen_done += 1
            color = {"OK": "#4ec9b0", "FAIL": "#f48771",
                     "SKIP": "#dcdcaa", "DRY": "#9cdcfe"}[parsed["state"]]
            text = (f"{parsed['state']}  {parsed['name']}"
                    + (f"  {parsed['bytes'] // 1024}KB {parsed['detail']}s"
                       if parsed["state"] == "OK" else f"  {parsed['detail'][:160]}"))
            self._log(text, color)
            self._refresh_progress()
            return
        color = "#f48771" if ("[warn]" in line or "FAIL" in line) else (
            "#dcdcaa" if line.startswith("done") else "#d4d4d4")
        self._log(line, color)

    def _refresh_progress(self):
        total = self.gen_total
        self.gen_progress.setText(
            f"出图中：{self.gen_done}/{total}" if total else f"出图中：{self.gen_done}")
        self.statusBar().showMessage(
            f"Agnes 出图：{self.gen_done}/{total or '?'} 完成，图片 -> {sampler.display_path(OUTDIR)}")

    def _finish_gen(self, code: int):
        self.gen_worker = None
        agnes.ABORT = False
        self._set_running(False)
        total = self.gen_total or self.gen_done
        self.gen_progress.setText(f"结束：{self.gen_done}/{total}，退出码 {code}")
        self._log(f"—— 结束，退出码 {code} ——", "#9cdcfe" if code == 0 else "#f48771")
        self.statusBar().showMessage(f"出图结束（退出码 {code}）｜ 图片 -> {sampler.display_path(OUTDIR)}")
        self.refresh_tree_state()
        if code != 0:
            QMessageBox.warning(self, "出图有失败项",
                                "详见出图日志，失败原因多为网关偶发断连或提示词被拒。")

    def stop_gen(self):
        if self.gen_worker is None:
            return
        if not QMessageBox.question(self, "停止出图",
                                    "确定终止当前出图任务？已完成的图片会保留。"):
            return
        self.gen_worker.stop()
        self._log("[stop] 已请求停止：在途请求等它回来，之后的任务不再派发", "#ce9178")
        self.gen_progress.setText("停止中…")

    def gen_queue_all(self):
        if not PROMPTS.exists():
            QMessageBox.information(self, "没有任务", "任务队列为空，先点「开始抽卡」")
            return
        args = (["--batch", "--prompts", str(PROMPTS), "--outdir", str(OUTDIR)]
                + gen_args(TIERS.get(self.tier_combo.currentText()),
                           RATIO_OPTS.get(self.ratio_combo.currentText()),
                           self.workers_spin.value(), self.overwrite_check.isChecked()))
        self._start_gen(args, "批量出图")

    def gen_selected(self):
        rec = self._selected_record()
        if not rec:
            return
        args = (["--prompt", rec["prompt"], "--out", f"{rec['variant_id']}.png",
                 "--outdir", str(OUTDIR)]
                + gen_args(TIERS.get(self.tier_combo.currentText()),
                           RATIO_OPTS.get(self.ratio_combo.currentText()),
                           overwrite=self.overwrite_check.isChecked()))
        self._start_gen(args, f"单条出图 {rec['variant_id']}")

    def _selected_record(self):
        items = self.table.selectedItems()
        if not items:
            QMessageBox.information(self, "提示", "先在结果列表中选择一条")
            return None
        row = items[0].row()
        vid = self.table.item(row, 0).text()
        rec = self.records.get(vid)
        if not rec:
            QMessageBox.critical(self, "错误", f"manifest 中找不到 {vid}")
            return None
        return rec

    def image_state(self, variant_id: str) -> str:
        return "已出图" if (OUTDIR / f"{variant_id}.png").exists() else "待出图"

    def refresh_tree_state(self):
        for row in range(self.table.rowCount()):
            vid = self.table.item(row, 0).text()
            self.table.item(row, 4).setText(self.image_state(vid))

    # ---------------- 抽卡 ----------------
    def run_draw(self):
        n = self.n_spin.value()
        seed = self.seed_edit.text().strip()
        if seed and not seed.isdigit():
            QMessageBox.critical(self, "参数错误", "seed 须为整数，留空表示随机")
            return
        pack = self.pack_combo.currentText()
        quality = QUALITIES.get(self.quality_combo.currentText())

        overrides = []
        for row in self.override_rows:
            k, v = row.data()
            if not k and not v:
                continue
            if k not in SLOT_KEYS:
                QMessageBox.critical(self, "参数错误",
                                     f"未知槽位：{k}（合法：{'/'.join(SLOT_KEYS)}）")
                return
            if not v:
                QMessageBox.critical(self, "参数错误", f"槽位 {k} 未填写值")
                return
            overrides.append((k, v))

        args = ["--n", str(n)]
        if pack != PACK_ANY:
            args += ["--pack", pack]
        if seed:
            args += ["--draw-seed", seed]
        if quality:
            args += ["--quality", quality]
        for k, v in overrides:
            args += ["--set", f"{k}={v}"]

        self.btn_draw.setEnabled(False)
        self.statusBar().showMessage("抽卡中…")
        self.draw_worker = DrawWorker(args)
        self.draw_worker.finished_ok.connect(self._on_draw_done)
        self.draw_worker.start()

    def _on_draw_done(self, proc):
        self.btn_draw.setEnabled(True)
        self.draw_worker = None
        info = parse_output(proc.stdout)
        self.records = load_records(MANIFEST)
        self.refresh_tree(info)

        parts = []
        if info["seed"]:
            parts.append(f"draw_seed={info['seed']}")
        parts.append(f"产出 {info['count']} 条")
        if info["fail"]:
            parts.append(f"失败：{info['fail']}")
        if proc.stderr.strip():
            parts.append(proc.stderr.strip().splitlines()[0])
        self.statusBar().showMessage(
            " · ".join(parts) + f" ｜ 任务队列 -> {sampler.display_path(PROMPTS)}")

        if info["fail"] and info["count"] == 0:
            QMessageBox.critical(self, "抽卡失败", info["fail"])
        elif info["count"]:
            self._log(f"[queue] 已写入 {info['count']} 条任务，可点「出图（选中）」或「出图：任务队列全部」",
                      "#9cdcfe")

    def refresh_tree(self, info=None):
        self.table.setRowCount(0)
        for r in (info["rows"] if info else []):
            row = self.table.rowCount()
            self.table.insertRow(row)
            self.table.setItem(row, 0, QTableWidgetItem(r["variant_id"]))
            self.table.setItem(row, 1, QTableWidgetItem(str(r["chars"])))
            self.table.setItem(row, 2, QTableWidgetItem(r["pack"]))
            self.table.setItem(row, 3, QTableWidgetItem(r["outfit"]))
            self.table.setItem(row, 4, QTableWidgetItem(self.image_state(r["variant_id"])))
            self.table.setItem(row, 5, QTableWidgetItem(r["warn"]))

    def refresh_records(self):
        self.records = load_records(MANIFEST)
        self.refresh_tree_state()
        self.statusBar().showMessage(f"已刷新 manifest（{len(self.records)} 条记录）")

    def on_select(self):
        items = self.table.selectedItems()
        if not items:
            return
        vid = self.table.item(items[0].row(), 0).text()
        rec = self.records.get(vid)
        if not rec:
            return
        self.preview.setPlainText(self.fmt_record(rec))

    @staticmethod
    def fmt_record(rec: dict) -> str:
        lines = [rec.get("prompt", ""), ""]
        lines.append(f"variant_id: {rec['variant_id']}")
        lines.append(f"draw_seed: {rec['draw_seed']} ｜ chars: {rec['chars']}"
                     + (f" ｜ WARN: {rec['warn']}" if rec.get("warn") else ""))
        slots = rec.get("slots", {})
        lines.append("slots: " + json.dumps(slots, ensure_ascii=False))
        return "\n".join(lines)

    def copy_prompt(self):
        content = self.preview.toPlainText().strip()
        if not content:
            QMessageBox.information(self, "提示", "先点击列表中的一条结果")
            return
        QApplication.clipboard().setText(content)
        self.statusBar().showMessage("提示词已复制到剪贴板")

    def finalize_high(self):
        rec = self._selected_record()
        if not rec:
            return
        slots = rec.get("slots", {})
        pack = slots.get("pack", "misc")
        new_seed = random.randrange(1, 10 ** 8)
        job = {"variant_id": rec["variant_id"], "prompt": rec["prompt"],
               "quality": "high", "out": f"{pack}-{rec.get('hash', 'xxxxxx')}-{new_seed}.png"}
        tier, ratio, _why = agnes.resolve_target(job, _QuietArgs(), _SPEC_DEFAULTS)
        job.update(size=tier, ratio=ratio)
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        with PROMPTS.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(job, ensure_ascii=False) + "\n")
        self._log(f"[final] {job['out']} 已入队（high→{tier} {ratio}）", "#4ec9b0")
        self.statusBar().showMessage(f"定稿任务已追加到 {sampler.display_path(PROMPTS)}（quality=high）")

    def open_prompts_dir(self):
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(PROMPTS.parent)))

    def open_outdir(self):
        OUTDIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(OUTDIR)))

    def closeEvent(self, event):
        if self.gen_worker is not None:
            if not QMessageBox.question(self, "正在出图",
                                        "出图任务仍在进行，确定退出？"):
                event.ignore()
                return
            self.gen_worker.stop()
            self.gen_worker.wait(3000)
        event.accept()


# ---------------------------------------------------------------------------
# 辅助：给 agnes.resolve_target 用的空覆盖对象
# ---------------------------------------------------------------------------
class _QuietArgs:
    size = ratio = quality = None
    spec = None
    key = key_file = None
    prompts = outdir = runlog = None


_SPEC_DEFAULTS = agnes.load_defaults(_QuietArgs())


# ---------------------------------------------------------------------------
# QSS 样式表
# ---------------------------------------------------------------------------
LIGHT_QSS = """
QWidget {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 10pt;
    color: #1f2937;
}
QMainWindow, QWidget#centralwidget {
    background: #f3f4f6;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget {
    background: transparent;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QFrame#leftPanel {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 10px;
}
QLabel#panelTitle {
    font-size: 13pt;
    font-weight: 700;
    color: #111827;
    padding: 2px 0;
}
QLabel#hintLabel {
    color: #6b7280;
    font-size: 9pt;
}
QLabel#keyLabel {
    font-size: 9pt;
    padding: 4px 0;
}
QGroupBox {
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: 600;
    color: #374151;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #4b5563;
}
QPushButton {
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 6px 14px;
    color: #374151;
}
QPushButton:hover {
    background: #f9fafb;
    border-color: #9ca3af;
}
QPushButton:pressed {
    background: #f3f4f6;
}
QPushButton:disabled {
    color: #9ca3af;
    background: #f9fafb;
    border-color: #e5e7eb;
}
QPushButton#primaryButton {
    background: #3b82f6;
    border: 1px solid #2563eb;
    color: #ffffff;
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#primaryButton:hover {
    background: #2563eb;
}
QPushButton#primaryButton:pressed {
    background: #1d4ed8;
}
QPushButton#primaryButton:disabled {
    background: #93c5fd;
    border-color: #93c5fd;
    color: #ffffff;
}
QComboBox, QLineEdit, QSpinBox {
    background: #ffffff;
    border: 1px solid #d1d5db;
    border-radius: 6px;
    padding: 5px 8px;
    selection-background-color: #3b82f6;
}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {
    border: 1px solid #3b82f6;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #ffffff;
    border: 1px solid #d1d5db;
    selection-background-color: #dbeafe;
    selection-color: #1e3a8a;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 18px;
    border: none;
    background: transparent;
}
QCheckBox {
    spacing: 6px;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #d1d5db;
    border-radius: 4px;
    background: #ffffff;
}
QCheckBox::indicator:checked {
    background: #3b82f6;
    border-color: #2563eb;
    image: none;
}
QTableWidget {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    gridline-color: #f3f4f6;
    selection-background-color: #dbeafe;
    selection-color: #1e3a8a;
    alternate-background-color: #f9fafb;
}
QHeaderView::section {
    background: #f3f4f6;
    border: none;
    border-bottom: 1px solid #e5e7eb;
    padding: 6px 8px;
    font-weight: 600;
    color: #374151;
}
QTextEdit {
    background: #ffffff;
    border: 1px solid #e5e7eb;
    border-radius: 8px;
    padding: 8px;
    selection-background-color: #dbeafe;
}
QPlainTextEdit#logView {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #374151;
    border-radius: 8px;
    padding: 8px;
    font-family: "Consolas", "Cascadia Code", monospace;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #cbd5e1;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #94a3b8;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #cbd5e1;
    border-radius: 5px;
    min-width: 30px;
}
QStatusBar {
    background: #ffffff;
    border-top: 1px solid #e5e7eb;
    color: #4b5563;
}
QSplitter::handle {
    background: #e5e7eb;
    height: 4px;
}
QSplitter::handle:hover {
    background: #9ca3af;
}
"""


DARK_QSS = """
QWidget {
    font-family: "Microsoft YaHei UI", "Segoe UI", sans-serif;
    font-size: 10pt;
    color: #c0caf5;
}
QMainWindow, QWidget#centralwidget {
    background: #1a1b26;
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollArea > QWidget {
    background: transparent;
}
QScrollArea > QWidget > QWidget {
    background: transparent;
}
QFrame#leftPanel {
    background: #24283b;
    border: 1px solid #414868;
    border-radius: 10px;
}
QLabel#panelTitle {
    font-size: 13pt;
    font-weight: 700;
    color: #e0e0ff;
    padding: 2px 0;
}
QLabel#hintLabel {
    color: #9aa5ce;
    font-size: 9pt;
}
QLabel#keyLabel {
    font-size: 9pt;
    padding: 4px 0;
}
QGroupBox {
    background: #1f2335;
    border: 1px solid #414868;
    border-radius: 8px;
    margin-top: 12px;
    padding-top: 8px;
    font-weight: 600;
    color: #a9b1d6;
}
QGroupBox::title {
    subcontrol-origin: margin;
    left: 10px;
    padding: 0 6px;
    color: #9aa5ce;
}
QPushButton {
    background: #24283b;
    border: 1px solid #414868;
    border-radius: 6px;
    padding: 6px 14px;
    color: #c0caf5;
}
QPushButton:hover {
    background: #2f334d;
    border-color: #565f89;
}
QPushButton:pressed {
    background: #3b4261;
}
QPushButton:disabled {
    color: #565f89;
    background: #1f2335;
    border-color: #2f334d;
}
QPushButton#primaryButton {
    background: #7aa2f7;
    border: 1px solid #7aa2f7;
    color: #1a1b26;
    font-weight: 600;
    padding: 8px 14px;
}
QPushButton#primaryButton:hover {
    background: #89b4fa;
}
QPushButton#primaryButton:pressed {
    background: #6c92e8;
}
QPushButton#primaryButton:disabled {
    background: #3b4261;
    border-color: #3b4261;
    color: #565f89;
}
QComboBox, QLineEdit, QSpinBox {
    background: #1f2335;
    border: 1px solid #414868;
    border-radius: 6px;
    padding: 5px 8px;
    color: #c0caf5;
    selection-background-color: #7aa2f7;
    selection-color: #1a1b26;
}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus {
    border: 1px solid #7aa2f7;
}
QComboBox::drop-down {
    border: none;
    width: 22px;
}
QComboBox QAbstractItemView {
    background: #24283b;
    border: 1px solid #414868;
    selection-background-color: #3b4261;
    selection-color: #e0e0ff;
    outline: none;
}
QSpinBox::up-button, QSpinBox::down-button {
    width: 18px;
    border: none;
    background: transparent;
}
QCheckBox {
    spacing: 6px;
    color: #c0caf5;
}
QCheckBox::indicator {
    width: 16px;
    height: 16px;
    border: 1px solid #414868;
    border-radius: 4px;
    background: #1f2335;
}
QCheckBox::indicator:checked {
    background: #7aa2f7;
    border-color: #7aa2f7;
}
QTableWidget {
    background: #1f2335;
    border: 1px solid #414868;
    border-radius: 8px;
    gridline-color: #2f334d;
    selection-background-color: #3b4261;
    selection-color: #e0e0ff;
    alternate-background-color: #24283b;
}
QHeaderView::section {
    background: #24283b;
    border: none;
    border-bottom: 1px solid #414868;
    padding: 6px 8px;
    font-weight: 600;
    color: #a9b1d6;
}
QTextEdit {
    background: #1f2335;
    color: #c0caf5;
    border: 1px solid #414868;
    border-radius: 8px;
    padding: 8px;
    selection-background-color: #3b4261;
}
QPlainTextEdit#logView {
    background: #15161e;
    color: #c0caf5;
    border: 1px solid #2f334d;
    border-radius: 8px;
    padding: 8px;
    font-family: "Consolas", "Cascadia Code", monospace;
}
QScrollBar:vertical {
    background: transparent;
    width: 10px;
    margin: 0;
}
QScrollBar::handle:vertical {
    background: #414868;
    border-radius: 5px;
    min-height: 30px;
}
QScrollBar::handle:vertical:hover {
    background: #565f89;
}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {
    height: 0;
}
QScrollBar:horizontal {
    background: transparent;
    height: 10px;
}
QScrollBar::handle:horizontal {
    background: #414868;
    border-radius: 5px;
    min-width: 30px;
}
QStatusBar {
    background: #24283b;
    border-top: 1px solid #414868;
    color: #9aa5ce;
}
QSplitter::handle {
    background: #414868;
    height: 4px;
}
QSplitter::handle:hover {
    background: #565f89;
}
"""


# ---------------------------------------------------------------------------
# 自检与入口
# ---------------------------------------------------------------------------
def selftest() -> int:
    sample_fail = (
        "第 1 次抽卡失败：用户覆盖 [outfit=奶白色棉质吊带睡裙, pose=蜷坐在飘窗垫上] "
        "命中黑名单（home），请更换覆盖值\n"
        "draw_seed=20260908  产出 0 条\nprompts -> P\nmanifest -> M\n"
    )
    info = parse_output(sample_fail)
    assert info["seed"] == "20260908" and info["count"] == 0
    assert "命中黑名单" in (info["fail"] or "")

    sample_ok = (
        "draw_seed=314159  产出 3 条\nprompts -> P\nmanifest -> M\n"
        "  home-20bc87-314159  224字  home / 落肩卫衣配打底裤\n"
        "  city-4398d0-4821  272字  city / 米色风衣配白衬衫与直筒长裤  [长度 272 超出 180-260]\n"
    )
    info2 = parse_output(sample_ok)
    assert info2["count"] == 3 and len(info2["rows"]) == 2
    assert info2["rows"][0]["variant_id"] == "home-20bc87-314159"
    assert info2["rows"][0]["pack"] == "home"
    assert info2["rows"][1]["warn"] == "长度 272 超出 180-260"

    assert parse_gen_line("UI|OK|a.png|5084569|106.0") == {
        "state": "OK", "name": "a.png", "bytes": 5084569, "detail": "106.0"}
    assert parse_gen_line("UI|FAIL|a.png|0|HTTP 401 bad key")["state"] == "FAIL"
    assert parse_gen_line("UI|SKIP|a.png")["state"] == "SKIP"
    assert parse_gen_line("batch  3 条  队列=x") is None
    assert RE_BATCH_TOTAL.match("batch  3 条  队列=x").group(1) == "3"
    assert gen_args("2K", "3:4", 4, True) == ["--size", "2K", "--ratio", "3:4",
                                              "--workers", "4", "--overwrite"]
    assert gen_args() == []
    print("selftest OK：输出解析逻辑正常")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    win = MainWindow()
    win.show()

    if "--smoke" in argv:
        QTimer.singleShot(0, app.quit)
        print("smoke OK：窗口构建成功")
        return app.exec()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
