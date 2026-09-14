#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写真提示词抽卡台 v1 —— 采样（sampler.py）+ 出图（agnes.py）的桌面界面。

用法：
    python ui.py            打开图形界面
    python ui.py --selftest 不开界面，验证输出解析逻辑
    python ui.py --smoke    构建窗口后立即销毁（自动化冒烟）

设计原则：本文件只负责界面、参数组装与任务派发。抽卡、去重、黑名单、跨槽校验、
字数告警交给 sampler.py（subprocess 调用）；出图、重试、落盘、记账交给 agnes.py。
产物路径不在此处重复定义，复用 sampler 的解析函数，三个文件读同一套配置。
出图耗时几十秒到几分钟，因此所有生图调用都在子线程里跑，UI 主线程只消费队列。
"""
from __future__ import annotations

import json
import contextlib
import io
import json
import os
import queue
import random
import re
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, simpledialog, ttk

import agnes
import sampler

# 路径与配置的单一事实源在 sampler.py / agnes.py，此处只做绑定。
# 打包成 EXE 后 PROJECT_ROOT 是 EXE 所在目录，SAMPLER/GEN 不再以源码形式存在，
# 出图与抽卡都走同进程函数调用，见 _spawn_sampler / _spawn_gen 的分支。
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


def parse_output(stdout: str) -> dict:
    """解析 sampler.py 的标准输出（与 CLI 打印格式绑定）。"""
    info = {"seed": None, "count": 0, "fail": None, "rows": []}
    for line in stdout.splitlines():
        m = RE_ROW.match(line)
        if m:
            info["rows"].append({
                "variant_id": m.group(1),
                "chars": int(m.group(2)),
                "pack": m.group(3),
                "outfit": m.group(4),
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
    """解析 agnes.py 的机器可读行；非 UI 行返回 None。

    格式：UI|OK|名字|字节|耗时 / UI|FAIL|名字|0|原因 / UI|SKIP|名字 / UI|DRY|名字
    """
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
    """读取 manifest.jsonl：variant_id -> 记录。"""
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
    """跑一次采样，返回带 stdout / stderr / returncode 的对象。

    源码模式开子进程（崩了不影响界面）；EXE 模式没有第二个 python 可开，
    改为同进程调用 sampler.main，并把它的 print 输出重定向回字符串。
    """
    if not FROZEN:
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        return subprocess.run(
            [sys.executable, str(SAMPLER)] + extra_args,
            capture_output=True, text=True, encoding="utf-8",
            cwd=str(PROJECT_ROOT), env=env,
        )
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = sampler.main(extra_args)
    except SystemExit as exc:  # argparse 出错会直接 exit
        code = exc.code if isinstance(exc.code, int) else 1
    except Exception as exc:  # noqa: BLE001 - 界面不能因采样崩掉
        err.write(f"{type(exc).__name__}: {exc}\n")
        code = 1
    return subprocess.CompletedProcess(extra_args, code, out.getvalue(), err.getvalue())


def gen_args(tier=None, ratio=None, workers=None, overwrite=False):
    """把界面选项翻译成 agnes.py 的命令行参数，出图与定稿共用同一份翻译。"""
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


class _LineSink:
    """把 print() 输出按行喂回消费队列，冻结模式下替代子进程的 stdout 管道。"""

    def __init__(self, sink_queue: queue.Queue):
        self.q = sink_queue
        self.buf = ""

    def write(self, text: str):
        self.buf += text
        while "\n" in self.buf:
            line, self.buf = self.buf.split("\n", 1)
            self.q.put(("line", line.rstrip("\r\n")))
        return len(text)

    def flush(self):
        if self.buf.strip():
            self.q.put(("line", self.buf.strip()))
        self.buf = ""

    def isatty(self):
        return False


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.records = load_records(MANIFEST)
        self.override_rows = []  # [{"frame", "key", "value"}]
        self.gen_proc: subprocess.Popen | None = None
        self.gen_queue: queue.Queue = queue.Queue()
        self.gen_done = 0
        self.gen_total = 0

        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_left()
        self._build_right()
        self._build_status()
        self.refresh_key_state()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- 左侧：参数面板 ----------
    def _build_left(self):
        left = ttk.Frame(self.root, padding=(10, 10, 6, 10))
        left.grid(row=0, column=0, sticky="ns")
        left.columnconfigure(0, weight=1)
        ttk.Label(left, text="抽卡参数", font=("Microsoft YaHei UI", 12, "bold")
                  ).grid(row=0, column=0, sticky="w")

        sample = ttk.LabelFrame(left, text=" 采样 ", padding=8)
        sample.grid(row=1, column=0, sticky="ew", pady=(6, 8))
        sample.columnconfigure(1, weight=1)

        def labeled(row, text, widget, tip=""):
            ttk.Label(sample, text=text).grid(row=row, column=0, sticky="w", pady=(6, 0))
            widget.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(2, 0))
            if tip:
                widget.tooltip = tip

        self.pack_var = tk.StringVar(value=PACK_ANY)
        labeled(0, "风格包", ttk.Combobox(sample, textvariable=self.pack_var,
                                         values=PACKS, state="readonly", width=26))
        self.n_var = tk.StringVar(value="5")
        labeled(2, "条数（1-20）", ttk.Spinbox(sample, from_=1, to=20,
                                              textvariable=self.n_var, width=28))
        self.seed_var = tk.StringVar(value="")
        labeled(4, "draw_seed（留空=随机）",
                ttk.Entry(sample, textvariable=self.seed_var, width=30))
        self.quality_var = tk.StringVar(value="medium（抽卡→1K）")
        labeled(6, "质量档", ttk.Combobox(sample, textvariable=self.quality_var,
                                         values=list(QUALITIES), state="readonly", width=26))

        ttk.Label(sample, text="槽位覆盖（值可填词库文案或任意文本）",
                  foreground="#6B7280").grid(row=8, column=0, columnspan=2,
                                             sticky="w", pady=(10, 2))
        self.override_box = ttk.Frame(sample)
        self.override_box.grid(row=9, column=0, columnspan=2, sticky="ew")
        self.add_override_row()
        ttk.Button(sample, text="+ 添加覆盖行",
                   command=self.add_override_row).grid(row=10, column=0, sticky="w", pady=(4, 0))

        self.btn_draw = ttk.Button(left, text="开始抽卡", command=self.run_draw)
        self.btn_draw.grid(row=2, column=0, sticky="ew", pady=(0, 12))

        # ---- Agnes 出图分组 ----
        gen = ttk.LabelFrame(left, text=" Agnes Image 2.5 Flash 出图 ", padding=8)
        gen.grid(row=3, column=0, sticky="ew")
        gen.columnconfigure(1, weight=1)

        self.tier_var = tk.StringVar(value="自动（按质量映射）")
        ttk.Label(gen, text="输出档位").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Combobox(gen, textvariable=self.tier_var, values=list(TIERS),
                     state="readonly", width=26).grid(row=1, column=0, columnspan=2,
                                                      sticky="ew", pady=(2, 0))
        self.ratio_var = tk.StringVar(value="自动（词库锁定 3:4）")
        ttk.Label(gen, text="宽高比").grid(row=2, column=0, columnspan=2,
                                           sticky="w", pady=(8, 0))
        ttk.Combobox(gen, textvariable=self.ratio_var, values=list(RATIO_OPTS),
                     state="readonly", width=26).grid(row=3, column=0, columnspan=2,
                                                      sticky="ew", pady=(2, 0))
        row4 = ttk.Frame(gen)
        row4.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(row4, text="并发").pack(side="left")
        self.workers_var = tk.IntVar(value=2)
        ttk.Spinbox(row4, from_=1, to=8, textvariable=self.workers_var,
                    width=4).pack(side="left", padx=(4, 12))
        self.overwrite_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(row4, text="覆盖已出图片",
                        variable=self.overwrite_var).pack(side="left")

        self.key_label = ttk.Label(gen, text="密钥：检测中…", foreground="#6B7280",
                                   wraplength=250, justify="left")
        self.key_label.grid(row=5, column=0, columnspan=2, sticky="w", pady=(10, 2))
        keyrow = ttk.Frame(gen)
        keyrow.grid(row=6, column=0, columnspan=2, sticky="ew")
        ttk.Button(keyrow, text="设置密钥", command=self.set_key).pack(side="left")
        ttk.Button(keyrow, text="重检", command=self.refresh_key_state
                   ).pack(side="left", padx=6)

        self.btn_gen_all = ttk.Button(gen, text="出图：任务队列全部", command=self.gen_queue_all)
        self.btn_gen_all.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(10, 4))
        self.btn_gen_stop = ttk.Button(gen, text="停止出图", command=self.stop_gen, state="disabled")
        self.btn_gen_stop.grid(row=8, column=0, columnspan=2, sticky="ew")

        ttk.Label(left, text="采样只写任务队列，出图由 agnes.py 消费；\n"
                             "重复组合会被 manifest 全量去重自动重抽。",
                  foreground="#6B7280", justify="left", wraplength=270
                  ).grid(row=4, column=0, sticky="w", pady=(10, 0))

    def add_override_row(self, key="", value=""):
        frame = ttk.Frame(self.override_box)
        kvar = tk.StringVar(value=key)
        vvar = tk.StringVar(value=value)
        combo = ttk.Combobox(frame, textvariable=kvar, values=SLOT_KEYS, width=9)
        entry = ttk.Entry(frame, textvariable=vvar, width=22)
        btn = ttk.Button(frame, text="✕", width=3,
                         command=lambda f=frame: self.remove_override_row(f))
        combo.pack(side="left", padx=(0, 3))
        entry.pack(side="left", padx=(0, 3))
        btn.pack(side="left")
        frame.pack(fill="x", pady=2)
        self.override_rows.append({"frame": frame, "key": kvar, "value": vvar})

    def remove_override_row(self, frame):
        for row in self.override_rows:
            if row["frame"] is frame:
                frame.destroy()
                self.override_rows.remove(row)
                return

    # ---------- 右侧：结果 + 日志（可拖拽上下分栏） ----------
    def _build_right(self):
        right = ttk.Frame(self.root, padding=(4, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        split = ttk.Panedwindow(right, orient="vertical")
        split.pack(fill="both", expand=True)
        split.add(self._build_results(split), weight=3)
        split.add(self._build_gen_log(split), weight=2)

    def _build_results(self, parent) -> ttk.Frame:
        pane = ttk.Frame(parent)
        pane.rowconfigure(2, weight=1)
        pane.rowconfigure(4, weight=1)
        pane.columnconfigure(0, weight=1)

        bar = ttk.Frame(pane)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="抽卡结果", font=("Microsoft YaHei UI", 12, "bold")
                  ).pack(side="left")
        ttk.Button(bar, text="刷新记录", command=self.refresh_records).pack(side="right")
        ttk.Button(bar, text="打开出图目录", command=self.open_outdir
                   ).pack(side="right", padx=4)
        ttk.Button(bar, text="打开任务队列目录", command=self.open_prompts_dir
                   ).pack(side="right", padx=4)

        cols = ("variant", "chars", "pack", "outfit", "img", "warn")
        headers = {"variant": "变体ID", "chars": "字数", "pack": "包",
                   "outfit": "服装", "img": "出图", "warn": "状态"}
        widths = {"variant": 200, "chars": 56, "pack": 86, "outfit": 220,
                  "img": 90, "warn": 200}
        self.tree = ttk.Treeview(pane, columns=cols, show="headings", height=8)
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "outfit"))
        vsb = ttk.Scrollbar(pane, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=2, column=0, sticky="nsew")
        vsb.grid(row=2, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        ttk.Label(pane, text="提示词预览（点击上方条目）",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=3, column=0,
                                                                sticky="w", pady=(10, 2))
        self.preview = tk.Text(pane, height=7, wrap="word",
                               font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        pvsb = ttk.Scrollbar(pane, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pvsb.set)
        self.preview.grid(row=4, column=0, sticky="nsew")
        pvsb.grid(row=4, column=1, sticky="ns")

        btns = ttk.Frame(pane)
        btns.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.btn_gen_one = ttk.Button(btns, text="出图（选中）", command=self.gen_selected)
        self.btn_gen_one.pack(side="left")
        ttk.Button(btns, text="复制提示词", command=self.copy_prompt).pack(side="left", padx=6)
        self.btn_final = ttk.Button(btns, text="定稿入队(high)", command=self.finalize_high)
        self.btn_final.pack(side="left")
        ttk.Label(btns, text=f"任务队列：{sampler.display_path(PROMPTS)}",
                  foreground="#6B7280").pack(side="left", padx=10)
        return pane

    def _build_gen_log(self, parent) -> ttk.Frame:
        pane = ttk.Frame(parent)
        pane.rowconfigure(1, weight=1)
        pane.columnconfigure(0, weight=1)

        head = ttk.Frame(pane)
        head.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 4))
        ttk.Label(head, text="出图日志", font=("Microsoft YaHei UI", 10, "bold")
                  ).pack(side="left")
        self.gen_progress = ttk.Label(head, text="空闲", foreground="#6B7280")
        self.gen_progress.pack(side="left", padx=12)
        ttk.Button(head, text="清空", command=lambda: self._log_clear()
                   ).pack(side="right")
        ttk.Button(head, text="打开图片目录", command=self.open_outdir).pack(side="right", padx=4)

        self.log = tk.Text(pane, height=8, wrap="none", relief="solid", borderwidth=1,
                           font=("Consolas", 9), state="disabled", background="#1E1E1E",
                           foreground="#D4D4D4", insertbackground="#D4D4D4")
        lvsb = ttk.Scrollbar(pane, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=lvsb.set)
        self.log.grid(row=1, column=0, sticky="nsew")
        lvsb.grid(row=1, column=1, sticky="ns")
        for tag, color in (("ok", "#4EC9B0"), ("fail", "#F48771"), ("skip", "#DCDCAA"),
                           ("warn", "#CE9178"), ("dim", "#9CDCFE")):
            self.log.tag_configure(tag, foreground=color)
        return pane

    def _build_status(self):
        self.status = tk.StringVar(value=f"就绪。词库：{sampler.display_path(SPEC)}")
        bar = ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")

    # ---------- 日志与进度 ----------
    def _log_clear(self):
        self.log.configure(state="normal")
        self.log.delete("1.0", tk.END)
        self.log.configure(state="disabled")

    def _log(self, text: str, tag: str = ""):
        self.log.configure(state="normal")
        self.log.insert(tk.END, text + "\n", tag or "")
        self.log.see(tk.END)
        self.log.configure(state="disabled")

    def _set_running(self, running: bool):
        """跑动时锁掉所有触发入口，只留「停止出图」可用。"""
        state = "disabled" if running else "normal"
        for btn in (self.btn_draw, self.btn_gen_one, self.btn_gen_all, self.btn_final):
            btn.configure(state=state)
        self.btn_gen_stop.configure(state="normal" if running else "disabled")

    # ---------- 出图 ----------
    def refresh_key_state(self):
        key, source = agnes.resolve_api_key()
        if key:
            self.key_label.configure(text=f"密钥：{agnes.mask_key(key)}\n来源：{source}",
                                     foreground="#1A7F3B")
        else:
            self.key_label.configure(
                text="密钥：未配置。点「设置密钥」，或写入项目根 agnes.key",
                foreground="#B91C1C")
        self.status.set(f"密钥状态已刷新：{'已配置' if key else '未配置'}")

    def set_key(self):
        key = simpledialog.askstring("设置 Agnes API Key", "粘贴 API Key：",
                                     show="*", parent=self.root)
        if not key or not key.strip():
            return
        target = agnes.set_api_key(key.strip())
        self.refresh_key_state()
        self._log(f"[key] 已写入 {sampler.display_path(target)}（已 gitignore）", "dim")

    def _start_gen(self, extra_args: list, title: str):
        if self.gen_proc is not None:
            messagebox.showinfo("出图", "已有出图任务在跑，先等它结束或点「停止出图」")
            return False
        key, _src = agnes.resolve_api_key()
        if not key:
            messagebox.showerror("缺少密钥", "先设置 API Key：左侧「Agnes 出图」→「设置密钥」")
            return False
        OUTDIR.mkdir(parents=True, exist_ok=True)
        if FROZEN:
            # EXE 里没有第二个解释器，只能在子线程里同进程跑 agnes.main，
            # 用 redirect_stdout 把 print 按行灌回同一个队列，消费端逻辑不变。
            agnes.ABORT = False
            worker = threading.Thread(target=self._run_gen_inprocess,
                                      args=(extra_args,), daemon=True)
            self.gen_proc = "inprocess"
            command = "[EXE 同进程] agnes.main " + " ".join(extra_args)
        else:
            env = dict(os.environ)
            env["PYTHONIOENCODING"] = "utf-8"
            try:
                proc = subprocess.Popen(
                    [sys.executable, str(GEN)] + extra_args,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", bufsize=1,
                    cwd=str(PROJECT_ROOT), env=env)
            except OSError as exc:
                messagebox.showerror("启动失败", str(exc))
                return False
            worker = threading.Thread(target=self._pump, args=(proc,), daemon=True)
            self.gen_proc = proc
            command = "python agnes.py " + " ".join(extra_args)
        self.gen_done = 0
        self.gen_total = 0
        self._log_clear()
        self._log(f"$ {command}", "dim")
        self._log(f"—— {title} ——", "dim")
        self._set_running(True)
        self.gen_progress.configure(text=f"{title}：0/…")
        worker.start()
        self.root.after(80, self._poll_gen)
        return True

    def _run_gen_inprocess(self, extra_args: list):
        """子线程只写队列，不碰 Tk 组件；跨线程改 UI 是 tkinter 的经典崩点。"""
        sink = _LineSink(self.gen_queue)
        code = 1
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                code = agnes.main(extra_args) or 0
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else 1
        except Exception as exc:  # noqa: BLE001 - 单条异常不该带走整个界面
            sink.write(f"[error] {type(exc).__name__}: {exc}\n")
        finally:
            sink.flush()
            self.gen_queue.put(("exit", code))

    def _pump(self, proc: subprocess.Popen):
        """子线程只搬运行，不碰 Tk 组件；跨线程改 UI 是 tkinter 的经典崩点。"""
        try:
            for line in proc.stdout:
                self.gen_queue.put(("line", line.rstrip("\n")))
        except (OSError, ValueError):
            pass
        finally:
            self.gen_queue.put(("exit", proc.wait()))

    def _poll_gen(self):
        if self.gen_proc is None:
            return
        finished = None
        while True:
            try:
                kind, payload = self.gen_queue.get_nowait()
            except queue.Empty:
                break
            if kind == "exit":
                finished = payload
                continue
            self._handle_line(payload)
        if finished is not None:
            self._finish_gen(finished)
            return
        self.root.after(120, self._poll_gen)

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
            tag = {"OK": "ok", "FAIL": "fail", "SKIP": "skip", "DRY": "dim"}[parsed["state"]]
            text = (f"{parsed['state']}  {parsed['name']}"
                    + (f"  {parsed['bytes'] // 1024}KB {parsed['detail']}s"
                       if parsed["state"] == "OK" else f"  {parsed['detail'][:160]}"))
            self._log(text, tag)
            self._refresh_progress()
            return
        tag = "fail" if ("[warn]" in line or "FAIL" in line) else (
            "skip" if line.startswith("done") else "")
        self._log(line, tag)

    def _refresh_progress(self):
        total = self.gen_total
        self.gen_progress.configure(
            text=f"出图中：{self.gen_done}/{total}" if total else f"出图中：{self.gen_done}")
        self.status.set(f"Agnes 出图：{self.gen_done}/{total or '?'} 完成，"
                        f"图片 -> {sampler.display_path(OUTDIR)}")

    def _finish_gen(self, code: int):
        self.gen_proc = None
        agnes.ABORT = False
        self._set_running(False)
        total = self.gen_total or self.gen_done
        self.gen_progress.configure(text=f"结束：{self.gen_done}/{total}，退出码 {code}")
        self._log(f"—— 结束，退出码 {code} ——", "dim" if code == 0 else "fail")
        self.status.set(f"出图结束（退出码 {code}）｜ 图片 -> {sampler.display_path(OUTDIR)}")
        self.refresh_tree_state()
        if code != 0:
            messagebox.showwarning("出图有失败项",
                                   "详见出图日志，失败原因多为网关偶发断连或提示词被拒。")

    def stop_gen(self):
        if self.gen_proc is None:
            return
        if not messagebox.askyesno("停止出图", "确定终止当前出图任务？已完成的图片会保留。"):
            return
        if self.gen_proc == "inprocess":
            agnes.ABORT = True
            self._log("[stop] 已请求停止：在途请求等它回来，之后的任务不再派发", "warn")
            self.gen_progress.configure(text="停止中…")
            return
        try:
            self.gen_proc.terminate()
        except OSError as exc:
            messagebox.showerror("停止失败", str(exc))

    def gen_queue_all(self):
        if not PROMPTS.exists():
            messagebox.showinfo("没有任务", "任务队列为空，先点「开始抽卡」")
            return
        args = (["--batch", "--prompts", str(PROMPTS), "--outdir", str(OUTDIR)]
                + gen_args(TIERS.get(self.tier_var.get()),
                           RATIO_OPTS.get(self.ratio_var.get()),
                           self.workers_var.get(), self.overwrite_var.get()))
        self._start_gen(args, "批量出图")

    def gen_selected(self):
        rec = self._selected_record()
        if not rec:
            return
        args = (["--prompt", rec["prompt"], "--out", f"{rec['variant_id']}.png",
                 "--outdir", str(OUTDIR)]
                + gen_args(TIERS.get(self.tier_var.get()),
                           RATIO_OPTS.get(self.ratio_var.get()),
                           overwrite=self.overwrite_var.get()))
        self._start_gen(args, f"单条出图 {rec['variant_id']}")

    def _selected_record(self):
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在结果列表中选择一条")
            return None
        vid = self.tree.item(sel[0])["values"][0]
        rec = self.records.get(vid)
        if not rec:
            messagebox.showerror("错误", f"manifest 中找不到 {vid}")
            return None
        return rec

    def image_state(self, variant_id: str) -> str:
        return "已出图" if (OUTDIR / f"{variant_id}.png").exists() else "待出图"

    def refresh_tree_state(self):
        for item in self.tree.get_children():
            values = list(self.tree.item(item)["values"])
            values[4] = self.image_state(str(values[0]))
            self.tree.item(item, values=values)

    # ---------- 抽卡 ----------
    def run_draw(self):
        n_str = self.n_var.get().strip()
        if not n_str.isdigit() or not (1 <= int(n_str) <= 20):
            messagebox.showerror("参数错误", "条数须为 1-20 的整数")
            return
        n = int(n_str)
        seed = self.seed_var.get().strip()
        if seed and not seed.isdigit():
            messagebox.showerror("参数错误", "seed 须为整数，留空表示随机")
            return
        pack = self.pack_var.get()
        quality = QUALITIES.get(self.quality_var.get())

        overrides = []
        for row in self.override_rows:
            k = row["key"].get().strip()
            v = row["value"].get().strip()
            if not k and not v:
                continue
            if k not in SLOT_KEYS:
                messagebox.showerror("参数错误", f"未知槽位：{k}（合法：{'/'.join(SLOT_KEYS)}）")
                return
            if not v:
                messagebox.showerror("参数错误", f"槽位 {k} 未填写值")
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

        self.btn_draw.config(state="disabled")
        self.status.set("抽卡中…")
        self.root.update_idletasks()
        try:
            proc = run_sampler(args)
        finally:
            self.btn_draw.config(state="normal")

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
        self.status.set(" · ".join(parts) + f" ｜ 任务队列 -> {sampler.display_path(PROMPTS)}")

        if info["fail"] and info["count"] == 0:
            messagebox.showerror("抽卡失败", info["fail"])
        elif info["count"]:
            self._log(f"[queue] 已写入 {info['count']} 条任务，可点「出图（选中）」或「出图：任务队列全部」",
                      "dim")

    def refresh_tree(self, info=None):
        self.tree.delete(*self.tree.get_children())
        for r in (info["rows"] if info else []):
            self.tree.insert("", "end", values=(
                r["variant_id"], r["chars"], r["pack"], r["outfit"],
                self.image_state(r["variant_id"]), r["warn"]))

    def refresh_records(self):
        self.records = load_records(MANIFEST)
        self.refresh_tree_state()
        self.status.set(f"已刷新 manifest（{len(self.records)} 条记录）")

    def on_select(self, _event=None):
        sel = self.tree.selection()
        if not sel:
            return
        vid = self.tree.item(sel[0])["values"][0]
        rec = self.records.get(vid)
        if not rec:
            return
        self.preview.delete("1.0", tk.END)
        self.preview.insert(tk.END, self.fmt_record(rec))

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
        content = self.preview.get("1.0", "end-1c").strip()
        if not content:
            messagebox.showinfo("提示", "先点击列表中的一条结果")
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        self.status.set("提示词已复制到剪贴板")

    def finalize_high(self):
        """把选中组合以 high（映射到 2K 档）追加进任务队列，不覆盖已有任务。"""
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
        self._log(f"[final] {job['out']} 已入队（high→{tier} {ratio}）", "ok")
        self.status.set(f"定稿任务已追加到 {sampler.display_path(PROMPTS)}（quality=high）")

    def open_prompts_dir(self):
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(str(PROMPTS.parent))

    def open_outdir(self):
        OUTDIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(OUTDIR))

    def on_close(self):
        if self.gen_proc is not None:
            if not messagebox.askyesno("正在出图", "出图任务仍在进行，确定退出？"):
                return
            if self.gen_proc != "inprocess":
                try:
                    self.gen_proc.terminate()
                except OSError:
                    pass
        self.root.destroy()


class _QuietArgs:
    """给 agnes.resolve_target 用的空覆盖对象：全走队列与词库语义。"""
    size = ratio = quality = None
    spec = None
    key = key_file = None
    prompts = outdir = runlog = None


_SPEC_DEFAULTS = agnes.load_defaults(_QuietArgs())


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

    root = tk.Tk()
    root.title("写真提示词抽卡台 v1 · Agnes 出图")
    root.geometry("1220x800")
    root.minsize(1040, 680)
    App(root)

    if "--smoke" in argv:
        root.withdraw()
        root.update_idletasks()
        print("smoke OK：窗口构建成功")
        root.destroy()
        return 0

    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
