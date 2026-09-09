#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""写真提示词抽卡台 v1 —— 包装 gacha/sampler.py 的桌面 UI。

用法：
    python ui.py            打开图形界面
    python ui.py --selftest 不开界面，验证输出解析逻辑
    python ui.py --smoke    构建窗口后立即销毁（自动化冒烟）

设计原则：本文件只负责界面与参数组装。抽卡、去重、黑名单、跨槽校验、
字数告警全部交给同目录的 sampler.py（subprocess 调用），不修改三件套。
产物与 sampler 一致：tmp/imagegen/prompts.jsonl（任务队列）、
output/imagegen/gacha/manifest.jsonl（记录 + 去重依据）。
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

HERE = Path(__file__).resolve().parent
SAMPLER = HERE / "sampler.py"
PROMPTS = HERE.parent / "tmp" / "imagegen" / "prompts.jsonl"
MANIFEST = HERE.parent / "output" / "imagegen" / "gacha" / "manifest.jsonl"

SLOT_KEYS = ["hair", "gaze", "anchor", "scene", "outfit", "pose", "light"]
PACK_ANY = "全部(轮转)"
PACKS = [PACK_ANY, "city", "home", "night", "retro", "cafe", "athleisure"]
QUALITIES = {"medium（抽卡）": "medium", "high（定稿）": "high", "默认": None}

RE_SEED = re.compile(r"^draw_seed=(\d+)\s+产出 (\d+) 条$")
RE_FAIL = re.compile(r"^第 (\d+) 次抽卡失败：(.*)$")
RE_ROW = re.compile(r"^\s{2}(\S+)\s+(\d+)字\s+(\w+) / (.+?)(?:\s+\[(.*)\])?$")


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


def load_records(path: Path) -> dict:
    """读取 manifest.jsonl：variant_id -> 记录。"""
    recs = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rec = json.loads(line)
                recs[rec["variant_id"]] = rec
    return recs


def run_sampler(extra_args):
    """调用 sampler.py（utf-8 双向，避免 Windows 控制台编码干扰）。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    return subprocess.run(
        [sys.executable, str(SAMPLER)] + extra_args,
        capture_output=True, text=True, encoding="utf-8",
        cwd=str(HERE), env=env,
    )


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.records = load_records(MANIFEST)
        self.override_rows = []  # [{"frame", "key", "value"}]

        root.option_add("*Font", ("Microsoft YaHei UI", 10))
        root.grid_rowconfigure(0, weight=1)
        root.grid_columnconfigure(1, weight=1)

        self._build_left()
        self._build_right()
        self._build_status()

    # ---------- 左侧：参数面板 ----------
    def _build_left(self):
        left = ttk.Frame(self.root, padding=10)
        left.grid(row=0, column=0, sticky="ns")
        ttk.Label(left, text="抽卡参数", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")

        ttk.Label(left, text="风格包").pack(anchor="w", pady=(8, 0))
        self.pack_var = tk.StringVar(value=PACK_ANY)
        ttk.Combobox(left, textvariable=self.pack_var, values=PACKS,
                     state="readonly", width=30).pack(fill="x")

        ttk.Label(left, text="条数（1-20）").pack(anchor="w", pady=(8, 0))
        self.n_var = tk.StringVar(value="5")
        ttk.Spinbox(left, from_=1, to=20, textvariable=self.n_var, width=30).pack(fill="x")

        ttk.Label(left, text="draw_seed（留空=随机）").pack(anchor="w", pady=(8, 0))
        self.seed_var = tk.StringVar(value="")
        ttk.Entry(left, textvariable=self.seed_var, width=32).pack(fill="x")

        ttk.Label(left, text="质量").pack(anchor="w", pady=(8, 0))
        self.quality_var = tk.StringVar(value="medium（抽卡）")
        ttk.Combobox(left, textvariable=self.quality_var, values=list(QUALITIES),
                     state="readonly", width=30).pack(fill="x")

        ttk.Label(left, text="槽位覆盖（可多行，值可填词库文案或任意文本）",
                  wraplength=300, justify="left").pack(anchor="w", pady=(8, 0))
        self.override_box = ttk.Frame(left)
        self.override_box.pack(fill="x")
        self.add_override_row()
        ttk.Button(left, text="+ 添加覆盖行", command=self.add_override_row).pack(anchor="w", pady=(4, 0))

        self.btn_draw = ttk.Button(left, text="开始抽卡", command=self.run_draw)
        self.btn_draw.pack(fill="x", pady=(12, 4))

        ttk.Label(left, text="提示：抽卡用 medium，定稿用 high；\n"
                             "重复组合会被 manifest 全量去重自动重抽。",
                  foreground="#6B7280", justify="left", wraplength=300).pack(anchor="w")

    def add_override_row(self, key="", value=""):
        frame = ttk.Frame(self.override_box)
        kvar = tk.StringVar(value=key)
        vvar = tk.StringVar(value=value)
        combo = ttk.Combobox(frame, textvariable=kvar, values=SLOT_KEYS, width=9)
        entry = ttk.Entry(frame, textvariable=vvar, width=24)
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

    # ---------- 右侧：结果区 ----------
    def _build_right(self):
        right = ttk.Frame(self.root, padding=(0, 10, 10, 10))
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_rowconfigure(1, weight=1)
        right.grid_rowconfigure(3, weight=1)
        right.grid_columnconfigure(0, weight=1)

        bar = ttk.Frame(right)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ttk.Label(bar, text="抽卡结果", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        ttk.Button(bar, text="刷新记录", command=self.refresh_records).pack(side="right")
        ttk.Button(bar, text="打开 manifest 目录", command=self.open_manifest_dir).pack(side="right", padx=4)
        ttk.Button(bar, text="打开任务队列目录", command=self.open_prompts_dir).pack(side="right")

        cols = ("variant", "chars", "pack", "outfit", "warn")
        headers = {"variant": "变体ID", "chars": "字数", "pack": "包",
                   "outfit": "服装", "warn": "状态"}
        widths = {"variant": 190, "chars": 56, "pack": 90, "outfit": 250, "warn": 220}
        self.tree = ttk.Treeview(right, columns=cols, show="headings", height=9)
        for c in cols:
            self.tree.heading(c, text=headers[c])
            self.tree.column(c, width=widths[c], anchor="w", stretch=(c == "outfit"))
        vsb = ttk.Scrollbar(right, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.grid(row=1, column=0, sticky="nsew")
        vsb.grid(row=1, column=1, sticky="ns")
        self.tree.bind("<<TreeviewSelect>>", self.on_select)

        ttk.Label(right, text="提示词预览（点击上方条目）",
                  font=("Microsoft YaHei UI", 10, "bold")).grid(row=2, column=0, sticky="w", pady=(10, 2))
        self.preview = tk.Text(right, height=10, wrap="word",
                               font=("Microsoft YaHei UI", 10), relief="solid", borderwidth=1)
        pvsb = ttk.Scrollbar(right, orient="vertical", command=self.preview.yview)
        self.preview.configure(yscrollcommand=pvsb.set)
        self.preview.grid(row=3, column=0, sticky="nsew")
        pvsb.grid(row=3, column=1, sticky="ns")

        btns = ttk.Frame(right)
        btns.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Button(btns, text="复制提示词", command=self.copy_prompt).pack(side="left")
        ttk.Button(btns, text="定稿重抽(high)", command=self.finalize_high).pack(side="left", padx=6)
        ttk.Label(btns, text="出图需对接 image_gen.py（未提供）：任务队列导出到 tmp/imagegen",
                  foreground="#6B7280").pack(side="left", padx=10)

    def _build_status(self):
        self.status = tk.StringVar(value="就绪。词库：gacha/slots.json")
        bar = ttk.Label(self.root, textvariable=self.status, relief="sunken", anchor="w")
        bar.grid(row=1, column=0, columnspan=2, sticky="ew")

    # ---------- 动作 ----------
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
        self.status.set(" · ".join(parts) + f" ｜ 任务队列 -> {PROMPTS}")

        if info["fail"] and info["count"] == 0:
            messagebox.showerror("抽卡失败", info["fail"])

    def refresh_tree(self, info=None):
        self.tree.delete(*self.tree.get_children())
        rows = info["rows"] if info else []
        for r in rows:
            self.tree.insert("", "end", values=(
                r["variant_id"], r["chars"], r["pack"], r["outfit"], r["warn"]))

    def refresh_records(self):
        self.records = load_records(MANIFEST)
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
        """把选中组合以 quality=high 写一条任务（同组合 hash 已在 manifest，
        绕过去重直接入队，由生图环节消费）。"""
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo("提示", "先在结果列表中选择一条")
            return
        vid = self.tree.item(sel[0])["values"][0]
        rec = self.records.get(vid)
        if not rec:
            messagebox.showerror("错误", f"manifest 中找不到 {vid}")
            return
        slots = rec.get("slots", {})
        pack = slots.get("pack", "misc")
        new_seed = random.randrange(1, 10 ** 8)
        job = {
            "prompt": rec["prompt"],
            "size": "1024x1536",
            "quality": "high",
            "out": f"{pack}-{rec.get('hash', 'xxxxxx')}-{new_seed}.png",
        }
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        PROMPTS.write_text(json.dumps(job, ensure_ascii=False) + "\n", encoding="utf-8")
        self.status.set(f"定稿任务已写入 {PROMPTS}（quality=high，同组合 {vid}）")
        messagebox.showinfo("定稿", f"已生成 high 质量任务：\n{PROMPTS}\n\n"
                                    f"（任务队列已更新，交由生图脚本消费）")

    def open_prompts_dir(self):
        PROMPTS.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(str(PROMPTS.parent))

    def open_manifest_dir(self):
        MANIFEST.parent.mkdir(parents=True, exist_ok=True)
        os.startfile(str(MANIFEST.parent))


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
    print("selftest OK：输出解析逻辑正常")
    return 0


def main(argv=None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if "--selftest" in argv:
        return selftest()

    root = tk.Tk()
    root.title("写真提示词抽卡台 v1")
    root.geometry("1160x720")
    root.minsize(980, 620)
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
