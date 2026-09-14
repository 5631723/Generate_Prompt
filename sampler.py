#!/usr/bin/env python3
"""词库采样器：slots.json -> prompts.jsonl + manifest.jsonl

只负责采样与拼装，不调用任何生图接口。
出图交给官方 image_gen.py 的 generate-batch 子命令。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import random
import sys
from pathlib import Path

# 路径约定：源码运行时本文件所在目录即项目根；打包成 EXE 后改为 EXE 所在目录，
# 这样产物（tmp/ output/ images/）永远落在用户看得见、可备份的地方，
# 而不是 PyInstaller 解包出来的 %TEMP%\_MEIxxxx 临时目录（退出即销毁）。
# 不在代码里写死绝对路径：相对路径一律按项目根展开（与 cwd 无关），
# 默认值可被 GACHA_SPEC / GACHA_PROMPTS / GACHA_MANIFEST 环境变量或 CLI 覆盖。


def runtime_root() -> Path:
    """EXE 模式取 EXE 所在目录，源码模式取仓库根。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def bundle_root() -> Path:
    """随 EXE 打包的只读资源目录；源码模式下与项目根相同。"""
    embedded = getattr(sys, "_MEIPASS", None)
    return Path(embedded).resolve() if embedded else runtime_root()


PROJECT_ROOT = runtime_root()
BUNDLE_ROOT = bundle_root()

DEFAULT_SPEC = "slots.json"
DEFAULT_PROMPTS = "tmp/imagegen/prompts.jsonl"
DEFAULT_MANIFEST = "output/imagegen/gacha/manifest.jsonl"
DEFAULT_IMAGES = "images"


def env_path(name: str, fallback: str) -> str:
    """读环境变量里的路径，留空或全空白时用 fallback。"""
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else fallback


def project_path(raw) -> Path:
    """绝对路径原样保留，相对路径按项目根展开。"""
    path = Path(str(raw)).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def spec_path(raw=None) -> Path:
    """词库定位：CLI 传参 > GACHA_SPEC > 项目根 slots.json > EXE 内置副本。

    项目根优先，是为了让打包后仍能直接改 EXE 旁边的 slots.json 调词库，
    不必重新打包；只有外部副本不存在时才退回内置的只读副本。
    """
    explicit = str(raw).strip() if raw else env_path("GACHA_SPEC", "").strip()
    if explicit:
        return project_path(explicit)
    external = PROJECT_ROOT / DEFAULT_SPEC
    if external.is_file():
        return external
    return BUNDLE_ROOT / DEFAULT_SPEC


def display_path(path) -> str:
    """落在项目根内就显示相对路径，否则显示原路径。"""
    resolved = project_path(path)
    try:
        return resolved.relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def entry_text(entry):
    return entry["t"] if isinstance(entry, dict) else entry


def resolve(pool, wanted):
    """把 --set 传入的值映射回词库条目，避免丢掉 detail 与 tags。"""
    for entry in pool:
        if isinstance(entry, dict):
            if entry.get("key") == wanted or entry.get("t") == wanted:
                return entry
        elif entry == wanted:
            return entry
    return {"t": wanted, "d": ""}


def pick(rng, pool, override):
    if override is not None:
        return resolve(pool, override)
    return rng.choice(pool)


def blocked_rule(spec, pack, outfit, pose, anchor, light):
    """命中返回黑名单规则，未命中返回 None。"""
    for rule in spec["blacklist"]:
        if rule.get("pack") and rule["pack"] != pack:
            continue
        if rule.get("outfit_tag") and rule["outfit_tag"] not in outfit.get("tags", []):
            continue
        if rule.get("pose_tag") and rule["pose_tag"] not in pose.get("tags", []):
            continue
        if rule.get("anchor") and anchor.get("key") != rule["anchor"]:
            continue
        if rule.get("light") and light.get("t") != rule["light"]:
            continue
        return rule
    return None


def compose(spec, s):
    t, l0 = spec["template"], spec["l0"]
    segments = [
        t["subject"].format(
            subject=l0["subject"], hair=s["hair"], gaze=s["gaze"], anchor=s["anchor"]
        ),
        t["body"].format(body=l0["body"]),
        t["pose"].format(pose=s["pose"]),
        t["outfit"].format(outfit=s["outfit"], outfit_detail=s["outfit_detail"]),
        t["scene"].format(scene=s["scene"], scene_detail=s["scene_detail"]),
        t["light"].format(light=s["light"]),
        t["tail"].format(camera=l0["camera"], purity=l0["purity"]),
    ]
    segments = [seg for seg in segments if seg.strip()]
    return "。".join(segments) + "。"


def slot_texts(spec, pack, picks):
    hair, gaze, anchor = picks["hair"], picks["gaze"], picks["anchor"]
    scene, outfit, pose, light = (
        picks["scene"],
        picks["outfit"],
        picks["pose"],
        picks["light"],
    )
    return {
        "pack": pack,
        "hair": entry_text(hair),
        "gaze": entry_text(gaze),
        "anchor": anchor["t"],
        "anchor_key": anchor.get("key", ""),
        "scene": scene["t"],
        "scene_detail": scene.get("d", ""),
        "outfit": outfit["t"],
        "outfit_detail": outfit.get("d", ""),
        "pose": pose["t"],
        "light": light["t"],
    }


def pack_has_need(spec, pack, need):
    """该风格包的场景/服装/姿态/光线（含细节 d）中是否存在所需道具词。"""
    texts = []
    for key in ("scene", "outfit", "pose", "light"):
        for entry in spec["packs"][pack][key]:
            if isinstance(entry, dict):
                texts.append(entry["t"] + entry.get("d", ""))
            else:
                texts.append(str(entry))
    return need in "".join(texts)


def draw_one(rng, spec, pack, overrides, used):
    """返回 (slot_dict, reason)。成功时 reason 为空串，失败时 reason 说明原因。"""
    pool_of = {
        "hair": spec["pools"]["hair"],
        "gaze": spec["pools"]["gaze"],
        "anchor": spec["pools"]["anchor"],
    }
    for _ in range(400):
        p = spec["packs"][pack]
        for key in ("scene", "outfit", "pose", "light"):
            pool_of[key] = p[key]
        picks = {k: pick(rng, pool_of[k], overrides.get(k)) for k in pool_of}

        rule = blocked_rule(spec, pack, picks["outfit"], picks["pose"], picks["anchor"], picks["light"])
        if rule is not None:
            # 若该规则涉及的可变槽位全部来自用户覆盖，重抽无法绕开 → 明确报错
            involved = []
            if "outfit_tag" in rule:
                involved.append("outfit")
            if "pose_tag" in rule:
                involved.append("pose")
            if "anchor" in rule:
                involved.append("anchor")
            if "light" in rule:
                involved.append("light")
            if involved and all(k in overrides for k in involved):
                detail = ", ".join(f"{k}={overrides[k]}" for k in involved)
                return None, f"用户覆盖 [{detail}] 命中黑名单（{pack}），请更换覆盖值"
            continue

        # 跨槽道具一致性：anchor 若依赖具体道具，前文（含细节描述 d）必须出现该道具
        need = picks["anchor"].get("requires")
        if need:
            context = "".join(
                entry_text(picks[k]) + (picks[k].get("d", "") if isinstance(picks[k], dict) else "")
                for k in ("scene", "outfit", "pose", "light")
            )
            if need not in context:
                if "anchor" in overrides:
                    if "scene" in overrides or not pack_has_need(spec, pack, need):
                        return None, (
                            f"用户覆盖 anchor={overrides['anchor']} 需要「{need}」出现在"
                            f"场景/服装/姿态/光线文本中，当前 {pack} 包无法满足，请更换覆盖值或风格包"
                        )
                    # 场景未覆盖且该包确实有该道具 → 换场景重抽
                    continue
                continue

        s = slot_texts(spec, pack, picks)
        h = hashlib.sha1(
            "|".join(f"{k}={s[k]}" for k in sorted(s)).encode("utf-8")
        ).hexdigest()[:6]
        if h in used:
            continue
        used.add(h)
        s["hash"] = h
        return s, ""
    return None, "词库空间耗尽（400 次尝试内未通过黑名单 / 跨槽校验 / 去重）"


def load_used(manifest_path: Path) -> set:
    used = set()
    if manifest_path.exists():
        for line in manifest_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                used.add(json.loads(line)["hash"])
    return used


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="词库采样器（不出图）")
    ap.add_argument("--spec", default=None,
                    help=f"词库路径，相对路径按项目根展开（默认 {DEFAULT_SPEC}）")
    ap.add_argument("--n", type=int, default=5)
    ap.add_argument("--draw-seed", type=int, default=None, dest="draw_seed")
    ap.add_argument("--pack", action="append", default=[], help="限定风格包，可重复")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="L2 用户覆盖，key 取 hair/gaze/anchor/scene/outfit/pose/light")
    ap.add_argument("--prompts", default=None,
                    help=f"任务队列输出，相对路径按项目根展开（默认 {DEFAULT_PROMPTS}）")
    ap.add_argument("--manifest", default=None,
                    help=f"记录表与去重依据，相对路径按项目根展开（默认 {DEFAULT_MANIFEST}）")
    ap.add_argument("--quality", default=None)
    ap.add_argument("--size", default=None)
    args = ap.parse_args(argv)

    # 优先级：CLI 显式传参 > GACHA_* 环境变量 > 项目根下的默认路径
    args.spec = spec_path(args.spec)
    args.prompts = project_path(
        env_path("GACHA_PROMPTS", DEFAULT_PROMPTS) if args.prompts is None else args.prompts
    )
    args.manifest = project_path(
        env_path("GACHA_MANIFEST", DEFAULT_MANIFEST) if args.manifest is None else args.manifest
    )

    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    packs = args.pack or list(spec["packs"].keys())
    for name in packs:
        if name not in spec["packs"]:
            print(f"未知风格包: {name}", file=sys.stderr)
            return 2

    overrides = {}
    valid_slots = {"hair", "gaze", "anchor", "scene", "outfit", "pose", "light"}
    for item in args.set:
        if "=" not in item:
            print(f"--set 需要 KEY=VALUE: {item}", file=sys.stderr)
            return 2
        k, v = item.split("=", 1)
        k = k.strip()
        if k not in valid_slots:
            print(f"警告：忽略未知覆盖槽位 {k}（合法：{'/'.join(sorted(valid_slots))}）", file=sys.stderr)
            continue
        overrides[k] = v.strip()
    if args.pack:
        overrides["__pack"] = args.pack[0]

    draw_seed = args.draw_seed if args.draw_seed is not None else random.randrange(1, 10**8)
    rng = random.Random(draw_seed)
    used = load_used(args.manifest)
    pack_queue: list = []

    lo, hi = spec["meta"]["char_range"]
    quality = args.quality or spec["meta"]["quality_draft"]
    size = args.size or spec["meta"]["size"]

    args.prompts.parent.mkdir(parents=True, exist_ok=True)
    args.manifest.parent.mkdir(parents=True, exist_ok=True)

    def next_pack():
        """一轮内风格包不重复，抽完再洗牌补下一轮。"""
        if not pack_queue:
            shuffled = list(packs)
            rng.shuffle(shuffled)
            pack_queue.extend(shuffled)
        return pack_queue.pop()

    jobs, records = [], []
    for i in range(args.n):
        pack = overrides.get("__pack") or next_pack()
        s, reason = draw_one(rng, spec, pack, overrides, used)
        if s is None:
            print(f"第 {i + 1} 次抽卡失败：{reason}")
            break
        prompt = compose(spec, {
            "hair": s["hair"], "gaze": s["gaze"], "anchor": s["anchor"],
            "scene": s["scene"], "scene_detail": s["scene_detail"],
            "outfit": s["outfit"], "outfit_detail": s["outfit_detail"],
            "pose": s["pose"], "light": s["light"],
        })
        variant_id = f"{s['pack']}-{s['hash']}-{draw_seed}"
        warn = "" if lo <= len(prompt) <= hi else f"长度 {len(prompt)} 超出 {lo}-{hi}"
        jobs.append({"prompt": prompt, "size": size, "quality": quality,
                     "out": f"{variant_id}.png"})
        records.append({"variant_id": variant_id, "draw_seed": draw_seed,
                        "hash": s["hash"], "chars": len(prompt), "warn": warn,
                        "slots": s, "prompt": prompt})

    args.prompts.write_text(
        "".join(json.dumps(j, ensure_ascii=False) + "\n" for j in jobs), encoding="utf-8")
    with args.manifest.open("a", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"draw_seed={draw_seed}  产出 {len(jobs)} 条")
    print(f"prompts -> {display_path(args.prompts)}")
    print(f"manifest -> {display_path(args.manifest)}")
    for r in records:
        flag = f"  [{r['warn']}]" if r["warn"] else ""
        print(f"  {r['variant_id']}  {r['chars']}字  {r['slots']['pack']} / {r['slots']['outfit']}{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
