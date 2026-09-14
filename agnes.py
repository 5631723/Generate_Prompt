#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Agnes Image 2.5 Flash 出图客户端：任务队列 -> 本地图片 + 出图日志。

职责边界：sampler.py 只采样与拼装，本文件只负责出图、落盘与记账，
两者通过任务队列（默认 tmp/imagegen/prompts.jsonl）单向通信。

API（2026-09-10 依据 https://agnes-ai.com/zh-Hans/docs/agnes-image-25-flash）：
  POST https://apihub.agnes-ai.com/v1/images/generations
  必填 model / prompt / size；size 用 1K-4K 档位再配 ratio 才有可预期画幅；
  图生图与多图合成的输入图像放在 extra_body.image（公共 URL 或 Data URI）；
  response_format 只能出现在 extra_body 内，顶层放置会被拒；无需 tags。
  响应取 data[0].url 或 data[0].b64_json；官方建议客户端超时 60-360s。

实测两条硬约束（2026-09-10，同一请求交替复现）：
  1. 网关会直接重置默认 User-Agent（Python-urllib/3.x）的连接，必须自带 UA；
  2. 长连接偶发 SSL UNEXPECTED_EOF（约 3-4 次一次），只能靠带抖动的退避重试兜。

用法：
    python agnes.py --selftest                       离线校验，不发网络
    python agnes.py --prompt "..." --out a.png       单条文生图
    python agnes.py --batch                          消费整个任务队列
    python agnes.py --batch --limit 2 --dry-run      只打印请求体，不发网络
    python agnes.py --batch --size 2K --ratio 3:4    强制档位与画幅
    python agnes.py --prompt "..." --image ref.png   图生图（本地文件自动转 Data URI）
    python agnes.py --status                         查看密钥与出图进度

密钥解析（不回显明文）：
    --key > 环境变量 AGNES_API_KEY > --key-file 指定的文件
          > AGNES_API_KEY_FILE > 项目根 agnes.key / .agnes.key
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import json
import mimetypes
import os
import random
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import sampler
from sampler import PROJECT_ROOT, display_path, env_path, project_path

ENDPOINT = "https://apihub.agnes-ai.com/v1/images/generations"
MODEL = "agnes-image-2.5-flash"
# 网关会掐掉 Python 默认 UA，这里必须是自定义值
USER_AGENT = "gacha-agnes/1.0"

DEFAULT_PROMPTS = "tmp/imagegen/prompts.jsonl"
DEFAULT_OUTDIR = "images"
DEFAULT_RUNLOG = "output/imagegen/gacha/runlog.jsonl"
DEFAULT_TIMEOUT = 300.0
TIMEOUT_RANGE = (60.0, 360.0)  # 官方建议区间

SIZE_TIERS = ["1K", "2K", "3K", "4K"]
RATIOS = ["1:1", "3:4", "4:3", "16:9", "9:16", "2:3", "3:2", "21:9"]

# 官方输出尺寸参考表（ratio -> 1K/2K/3K/4K 的精确像素），用于换算与自查
OUTPUT_SIZES = {
    "1:1": ["1024x1024", "2048x2048", "3072x3072", "4096x4096"],
    "3:4": ["864x1152", "1728x2304", "2592x3456", "3456x4608"],
    "4:3": ["1152x864", "2304x1728", "3456x2592", "4608x3456"],
    "16:9": ["1312x736", "2624x1472", "3936x2208", "5248x2944"],
    "9:16": ["736x1312", "1472x2624", "2208x3936", "2944x5248"],
    "2:3": ["832x1248", "1664x2496", "2496x3744", "3328x4992"],
    "3:2": ["1248x832", "2496x1664", "3744x2496", "4992x3328"],
    "21:9": ["1568x672", "3136x1344", "4704x2016", "6272x2688"],
}

# 历史精确尺寸的字面宽高比（gpt-image-2 那套 size 枚举）。
# 注意 1024x1536 字面是 2:3，而本项目 L0 锁死「竖构图 3:4」；
# 词库 meta.image_api.ratio 会在同方向上覆盖这个字面值，见 resolve_target。
SIZE_MAP = {
    "1024x1024": "1:1",
    "1024x1536": "2:3",
    "1536x1024": "3:2",
    "1024x768": "4:3",
    "768x1024": "3:4",
    "1024x1792": "9:16",
    "1792x1024": "16:9",
}

# Agnes 没有 quality 参数，用本项目的抽卡/定稿语义映射到分辨率档位
QUALITY_TIER = {"auto": "1K", "low": "1K", "medium": "1K",
                "standard": "1K", "high": "2K"}

RE_LEGACY_SIZE = re.compile(r"^(\d+)\s*[x×*]\s*(\d+)$", re.I)
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"RIFF", b"GIF8")

# 冻结模式（EXE 内同进程调用）下由 UI 置位：批量任务在派发下一条前检查。
# 已经发出去的请求取消不了，只能等它回来；这里保证的是“不再开新的”。
ABORT = False


def warn(msg: str) -> None:
    print(f"[warn] {msg}", file=sys.stderr)


def _utf8_console() -> None:
    """Windows 控制台默认 gbk，中文与箭头会炸；调用方（含 ui.py）按 utf-8 读。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, OSError):
            pass


# ---------- 配置与密钥 ----------
def spec_defaults(spec_path) -> dict:
    """词库 meta.image_api 是出图默认值的单一事实源，缺失时用内置兜底。"""
    out = {"model": MODEL, "size": "1K", "ratio": "", "timeout": DEFAULT_TIMEOUT}
    try:
        spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        warn(f"读不到词库 {display_path(spec_path)}（{exc.__class__.__name__}），用内置默认值")
        return out
    api = (spec.get("meta") or {}).get("image_api") or {}
    for key in ("model", "size", "ratio"):
        if api.get(key):
            out[key] = str(api[key]).strip()
    # 词库用 size_draft / size_final 表达抽卡与定稿两档，无质量语义时退回 draft
    if not out.get("size"):
        out["size"] = str(api.get("size_draft") or "").strip() or out["size"]
    try:
        if api.get("timeout"):
            out["timeout"] = float(api["timeout"])
    except (TypeError, ValueError):
        pass
    return out


def key_candidates(explicit_file: str | None = None) -> list:
    """密钥文件的搜索顺序：显式 --key-file > AGNES_API_KEY_FILE > 项目根约定名。"""
    if explicit_file and explicit_file.strip():
        return [project_path(explicit_file.strip())]
    pointer = os.environ.get("AGNES_API_KEY_FILE", "").strip()
    if pointer:
        return [project_path(pointer)]
    return [PROJECT_ROOT / "agnes.key", PROJECT_ROOT / ".agnes.key"]


def read_key_file(path) -> str:
    """取密钥文件首个非注释行，容忍 BOM、行尾空白与尾随注释。"""
    try:
        raw = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in raw.splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            return line.split()[0].strip()
    return ""


def resolve_api_key(explicit: str | None = None,
                    key_file: str | None = None) -> tuple[str, str]:
    """返回 (密钥, 来源)。未配置时返回 ("", "")。"""
    if explicit and explicit.strip():
        return explicit.strip(), "--key"
    env_val = os.environ.get("AGNES_API_KEY", "").strip()
    if env_val:
        return env_val, "env:AGNES_API_KEY"
    for path in key_candidates(key_file):
        if path.is_file():
            key = read_key_file(path)
            if key:
                return key, f"file:{display_path(path)}"
    return "", ""


def mask_key(key: str) -> str:
    if not key:
        return "<未配置>"
    if len(key) <= 10:
        return key[:2] + "***"
    return f"{key[:6]}…{key[-4:]}（{len(key)} 位）"


def set_api_key(key: str, path=None) -> Path:
    """写入项目根 agnes.key（已在 .gitignore），权限尽量收紧。"""
    target = Path(path) if path else PROJECT_ROOT / "agnes.key"
    target.write_text(key.strip() + "\n", encoding="utf-8")
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    return target


# ---------- 尺寸换算 ----------
def _ratio_value(ratio: str) -> float:
    width, _, height = str(ratio).partition(":")
    try:
        return float(width) / float(height)
    except (TypeError, ValueError):
        return 1.0


def _orientation(ratio: str) -> int:
    value = _ratio_value(ratio)
    return 1 if value > 1.02 else (-1 if value < 0.98 else 0)


def _pixel_area(size_str: str) -> float:
    match = RE_LEGACY_SIZE.match(str(size_str or "").strip())
    if not match:
        return 0.0
    return float(match.group(1)) * float(match.group(2))


def output_size(ratio: str, tier: str) -> str:
    """查官方参考表，得到 ratio+tier 的真实像素。"""
    try:
        return OUTPUT_SIZES[ratio][SIZE_TIERS.index(tier)]
    except (KeyError, ValueError):
        return "?"


def nearest_ratio(width: float, height: float) -> str:
    """未知精确尺寸：按宽高比挑最接近的支持档位。"""
    want = width / height
    return min(RATIOS, key=lambda r: abs(_ratio_value(r) - want))


def nearest_tier(ratio: str, area: float) -> str:
    """未知精确尺寸：按像素面积挑最接近的输出档位。"""
    if area <= 0:
        return SIZE_TIERS[0]
    return min(SIZE_TIERS, key=lambda t: abs(_pixel_area(output_size(ratio, t)) - area))


def _clean_tier(value) -> str:
    raw = str(value or "").strip().upper()
    return raw if raw in SIZE_TIERS else ""


def _clean_ratio(value) -> str:
    raw = str(value or "").strip().replace("：", ":")
    return raw if raw in RATIOS else ""


def legacy_ratio(size_value) -> str:
    """历史精确尺寸 -> 宽高比；表里没有就就近取。"""
    match = RE_LEGACY_SIZE.match(str(size_value or "").strip())
    if not match:
        return ""
    width, height = float(match.group(1)), float(match.group(2))
    return SIZE_MAP.get(f"{int(width)}x{int(height)}") or nearest_ratio(width, height)


def normalize_size(value, defaults: dict) -> str:
    """size 归一为档位：档位直接用；精确尺寸按字面宽高比 + 面积就近折算。"""
    raw = str(value or "").strip()
    if not raw:
        return _clean_tier(defaults.get("size")) or SIZE_TIERS[0]
    tier = _clean_tier(raw)
    if tier:
        return tier
    match = RE_LEGACY_SIZE.match(raw)
    if match:
        width, height = float(match.group(1)), float(match.group(2))
        return nearest_tier(legacy_ratio(raw) or nearest_ratio(width, height), width * height)
    warn(f"无法识别的 size={raw!r}，退回默认档位")
    return _clean_tier(defaults.get("size")) or SIZE_TIERS[0]


def derive_ratio(size_value, defaults: dict) -> tuple[str, str]:
    """无显式 ratio 时的推导，返回 (ratio, 来源标签)。

    词库 meta.image_api.ratio 代表 L0 锁死的画幅语义，优先级高于历史 size 的字面比例，
    但只在横竖方向一致时接管 —— 把横版 1536x1024 强行掰成 3:4 竖版属于回归。
    """
    spec_ratio = _clean_ratio(defaults.get("ratio"))
    literal = legacy_ratio(size_value)
    if spec_ratio and (not literal or _orientation(literal) == _orientation(spec_ratio)):
        return spec_ratio, "spec"
    if literal:
        return literal, "job:size-map"
    return spec_ratio or "1:1", "spec"


def resolve_target(job: dict, args, defaults: dict) -> tuple[str, str, str]:
    """按 CLI > 环境变量 > 任务队列 > 质量语义 > 词库 的优先级定档位与画幅。"""
    env_size = _clean_tier(os.environ.get("AGNES_SIZE", ""))
    env_ratio = _clean_ratio(os.environ.get("AGNES_RATIO", ""))
    job_size = str(job.get("size") or "").strip()
    chain = [
        ("cli", _clean_tier(args.size)),
        ("env", env_size),
        ("job", _clean_tier(job_size)),
        ("cli:quality", QUALITY_TIER.get(str(args.quality or "").strip().lower(), "")),
        ("job:quality", QUALITY_TIER.get(str(job.get("quality") or "").strip().lower(), "")),
    ]
    size = size_src = ""
    for src, value in chain:
        if value:
            size, size_src = value, src
            break
    if not size:
        size, size_src = (normalize_size(job_size, defaults), "job:size-map") \
            if job_size else (normalize_size(None, defaults), "spec")

    for src, value in (("cli", _clean_ratio(args.ratio)), ("env", env_ratio),
                       ("job", _clean_ratio(job.get("ratio")))):
        if value:
            return size, value, f"size={size}({size_src}) ratio={value}({src})"
    ratio, ratio_src = derive_ratio(job_size, defaults)
    return size, ratio, f"size={size}({size_src}) ratio={ratio}({ratio_src})"


# ---------- 请求构造 ----------
def image_ref(item: str) -> str:
    """图生图输入：公共 URL / Data URI 原样透传，本地文件转 Data URI Base64。"""
    raw = str(item).strip().strip('"')
    if raw.lower().startswith(("http://", "https://", "data:")):
        return raw
    path = project_path(raw)
    if not path.is_file():
        raise FileNotFoundError(f"参考图不存在：{raw}")
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode("ascii")


def build_body(prompt: str, size: str, ratio: str, images=None,
               model: str = MODEL) -> dict:
    """文生图取 URL 输出；图生图/多图合成把输入塞进 extra_body.image。

    response_format 只出现在 extra_body 内 —— 顶层放置会被官方文档明确判为错误写法。
    """
    refs = [image_ref(item) for item in (images or [])]
    body: dict = {"model": model, "prompt": prompt, "size": size, "ratio": ratio}
    extra: dict = {"response_format": "url"}
    if refs:
        extra["image"] = refs
    body["extra_body"] = extra
    return body


def brief_body(body: dict) -> dict:
    """打印请求体时截断 Data URI，避免几百 KB Base64 糊满终端。"""
    out = json.loads(json.dumps(body, ensure_ascii=False))
    refs = (out.get("extra_body") or {}).get("image") or []
    out["extra_body"]["image"] = [
        ref[:48] + f"…<{len(ref)} 字符 Data URI>"
        if ref.startswith("data:") and len(ref) > 64 else ref
        for ref in refs
    ]
    return out


# ---------- HTTP ----------
def _context(insecure: bool):
    if not insecure:
        return None
    warn("已跳过 TLS 证书校验（--insecure）")
    return ssl._create_unverified_context()


def post_json(payload: dict, api_key: str, timeout: float,
              insecure: bool = False) -> dict:
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        ENDPOINT, data=data, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": USER_AGENT,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_context(insecure)) as resp:
        raw = resp.read().decode("utf-8", "replace")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"响应不是合法 JSON：{raw[:400]}") from exc


def download(url: str, dest: Path, timeout: float, insecure: bool = False) -> int:
    """先写 .part 再原子改名，中途断连不会留下半个文件被误判为已出图。"""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    last = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=_context(insecure)) as resp:
                blob = resp.read()
            break
        except Exception as exc:  # noqa: BLE001
            last = exc
            time.sleep(min(12.0, 2.0 ** attempt + random.uniform(0, 1)))
    else:
        raise RuntimeError(f"图片下载失败：{last}")
    if not blob.startswith(IMAGE_MAGIC):
        raise RuntimeError(f"下载内容不像图片（前 16 字节 {blob[:16]!r}）")
    tmp.write_bytes(blob)
    tmp.replace(dest)
    return len(blob)


def generate(body: dict, *, api_key: str, timeout: float, retries: int = 3,
             insecure: bool = False) -> dict:
    """带抖动的退避重试；4xx（参数/鉴权错）不重试，重试只会白烧配额。"""
    last = None
    for attempt in range(retries + 1):
        try:
            return post_json(body, api_key, timeout, insecure)
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500] if exc.fp else str(exc)
            message = f"HTTP {exc.code} {exc.reason}: {detail.strip()}"
            if 400 <= exc.code < 500 and exc.code != 429:
                raise RuntimeError(message) from None
            last = message
        except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            last = f"网络异常：{type(exc).__name__}: {reason}"
        except RuntimeError as exc:
            last = str(exc)
        if attempt < retries:
            backoff = min(20.0, 2.0 ** attempt * 2 + random.uniform(0, 1.5))
            warn(f"出图失败（{last}），{backoff:.0f}s 后重试 {attempt + 1}/{retries}")
            time.sleep(backoff)
    raise RuntimeError(f"重试 {retries} 次后仍失败：{last}")


def extract(result: dict) -> tuple[str, str]:
    """返回 (kind, value)：URL 输出取 data[0].url，Base64 输出取 data[0].b64_json。"""
    data = result.get("data")
    first = data[0] if isinstance(data, list) and data else \
        (data if isinstance(data, dict) else {})
    for key in ("url", "image_url"):
        value = first.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return "url", value
    if first.get("b64_json"):
        return "b64", str(first["b64_json"])
    keys = " / ".join(sorted(k for k in first if not isinstance(first[k], dict))[:8])
    raise RuntimeError(f"响应里没有 url / b64_json，实际字段：{keys or '无'}")


def suffix_for(url: str) -> str:
    tail = Path(str(url).split("?")[0]).suffix.lower()
    return tail if tail in {".png", ".jpg", ".jpeg", ".webp"} else ".png"


# ---------- 出图 ----------
def out_path_for(job: dict, index: int, outdir: Path) -> Path:
    raw = str(job.get("out") or "").strip()
    if not raw:
        raw = f"{job.get('variant_id') or 'job'}-{index + 1}.png"
    name = Path(raw).name
    return outdir / (name if Path(name).suffix else name + ".png")


def run_job(job: dict, args, api_key: str, defaults: dict, index: int,
            outdir: Path | None = None) -> dict:
    prompt = str(job.get("prompt") or "").strip()
    size, ratio, why = resolve_target(job, args, defaults)
    record = {"index": index, "out": str(job.get("out") or ""), "size": size,
              "ratio": ratio, "size_source": why, "prompt_chars": len(prompt),
              "ok": False, "elapsed": 0.0}
    if not prompt:
        record["error"] = "任务缺少 prompt 字段"
        return record
    images = list(args.image or []) + [str(v) for v in (job.get("image") or [])]
    try:
        body = build_body(prompt, size, ratio, images,
                          model=args.model or defaults.get("model") or MODEL)
    except FileNotFoundError as exc:
        record["error"] = str(exc)
        return record

    if args.dry_run:
        record.update(ok=True, dry_run=True, request=brief_body(body))
        return record

    dest = out_path_for(job, index, outdir or outdir_path(args))
    if dest.exists() and not args.overwrite:
        record.update(ok=True, skipped=True, file=display_path(dest),
                      bytes=dest.stat().st_size)
        return record

    started = time.time()
    try:
        result = generate(body, api_key=api_key, timeout=args.timeout,
                          retries=args.retries, insecure=args.insecure)
        kind, value = extract(result)
        if kind == "url":
            nbytes = download(value, dest, args.timeout, args.insecure)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            blob = base64.b64decode(value)
            dest.write_bytes(blob)
            nbytes = len(blob)
        record.update(ok=True, file=display_path(dest), bytes=nbytes,
                      task_id=result.get("task_id") or "",
                      source=value if kind == "url" else "b64_json",
                      elapsed=round(time.time() - started, 1))
    except Exception as exc:  # noqa: BLE001 - 单条失败不该中断整批
        record["error"] = str(exc)[:500]
        record["elapsed"] = round(time.time() - started, 1)
    return record


def read_jobs(path: Path, limit: int | None = None) -> list:
    if not path.exists():
        raise FileNotFoundError(f"找不到任务队列：{path}（先跑 python sampler.py --n 5）")
    jobs = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            jobs.append(json.loads(line))
        except json.JSONDecodeError:
            warn(f"队列第 {lineno} 行不是合法 JSON，已跳过")
    return jobs[:limit] if limit else jobs


def _path_of(args, attr: str, env_name: str, fallback: str) -> Path:
    """CLI 显式值 > 环境变量 > 默认值；空串按“未填”处理，避免 project_path('') 。"""
    raw = (getattr(args, attr, None) or "").strip() if args is not None else ""
    return project_path(raw or env_path(env_name, fallback))


def queue_path(args=None) -> Path:
    return _path_of(args, "prompts", "AGNES_PROMPTS", DEFAULT_PROMPTS)


def outdir_path(args=None) -> Path:
    return _path_of(args, "outdir", "AGNES_OUTDIR", DEFAULT_OUTDIR)


def runlog_path(args=None) -> Path:
    return _path_of(args, "runlog", "AGNES_RUNLOG", DEFAULT_RUNLOG)


def load_defaults(args) -> dict:
    return spec_defaults(sampler.spec_path(getattr(args, "spec", None)))


def job_state(job: dict, outdir: Path, index: int) -> dict:
    dest = out_path_for(job, index, outdir)
    exists = dest.is_file()
    return {"out": dest.name, "file": display_path(dest), "exists": exists,
            "bytes": dest.stat().st_size if exists else 0,
            "variant_id": str(job.get("out") or dest.name).rsplit(".", 1)[0]}


# ---------- 输出格式 ----------
def format_result(record: dict) -> str:
    tag = record.get("out") or f"#{record.get('index', 0) + 1}"
    if record.get("dry_run"):
        return (f"DRY  {tag}  {record['size']} {record['ratio']}  "
                f"{record['size_source']}")
    if record.get("skipped"):
        return f"SKIP {tag}  已存在 -> {record['file']}"
    if record.get("ok"):
        kb = record.get("bytes", 0) / 1024
        return (f"OK   {tag}  {output_size(record['ratio'], record['size'])}  "
                f"{kb:.0f}KB  {record.get('elapsed', 0)}s  -> {record['file']}")
    return f"FAIL {tag}  {record.get('error', '未知错误')}"


def ui_line(record: dict) -> str:
    """给 ui.py 状态行用的机器可读前缀，字段里不含竖线。"""
    tag = (record.get("out") or f"job{record.get('index', 0)}").replace("|", "/")
    if record.get("dry_run"):
        return f"UI|DRY|{tag}"
    if record.get("skipped"):
        return f"UI|SKIP|{tag}"
    if record.get("ok"):
        return f"UI|OK|{tag}|{record.get('bytes', 0)}|{record.get('elapsed', 0)}"
    reason = str(record.get("error", "未知错误")).replace("|", "/").replace("\n", " ")
    return f"UI|FAIL|{tag}|0|{reason[:120]}"


def append_runlog(path: Path, records: list) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as exc:
        warn(f"出图日志写入失败：{exc}")


# ---------- 命令 ----------
def cmd_batch(args, api_key: str, defaults: dict) -> int:
    queue = queue_path(args)
    try:
        jobs = read_jobs(queue, args.limit)
    except FileNotFoundError as exc:
        print(f"FAIL {exc}")
        return 1
    if not jobs:
        print(f"任务队列为空：{display_path(queue)}")
        return 1
    outdir = outdir_path(args)
    outdir.mkdir(parents=True, exist_ok=True)
    print(f"batch  {len(jobs)} 条  队列={display_path(queue)}  输出={display_path(outdir)}")

    started = time.time()
    records: list = []
    workers = 1 if args.dry_run else max(1, args.workers)
    if workers == 1:
        for index, job in enumerate(jobs):
            if ABORT:
                print("ABORT 已停止派发后续任务")
                break
            rec = run_job(job, args, api_key, defaults, index, outdir)
            records.append(rec)
            print(format_result(rec), flush=True)
            print(ui_line(rec), flush=True)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            pending = {}
            for i, job in enumerate(jobs):
                if ABORT:
                    print("ABORT 已停止派发后续任务")
                    break
                pending[pool.submit(run_job, job, args, api_key, defaults, i, outdir)] = i
            done = 0
            for future in concurrent.futures.as_completed(pending):
                rec = future.result()
                records.append(rec)
                done += 1
                print(f"[{done}/{len(jobs)}] ", end="", flush=True)
                print(format_result(rec), flush=True)
                print(ui_line(rec), flush=True)

    records.sort(key=lambda r: r.get("index", 0))
    ok = sum(1 for r in records if r.get("ok"))
    fail = len(records) - ok
    if not args.dry_run:
        log = runlog_path(args)
        append_runlog(log, [dict(r, ts=int(time.time()), endpoint=ENDPOINT,
                                 model=args.model or defaults.get("model"))
                            for r in records])
        print(f"log    -> {display_path(log)}")
    print(f"done   {ok} 成功 / {fail} 失败  用时 {time.time() - started:.0f}s")
    return 0 if fail == 0 else 1


def cmd_single(args, api_key: str, defaults: dict) -> int:
    prompt = args.prompt or ""
    if not prompt.strip() and args.prompt_file:
        prompt = project_path(args.prompt_file).read_text(encoding="utf-8")
    prompt = prompt.strip()
    if not prompt:
        print("需要 --prompt 或 --prompt-file", file=sys.stderr)
        return 2
    job = {"prompt": prompt, "out": args.out or "", "size": args.size or "",
           "ratio": args.ratio or "", "quality": args.quality or ""}
    rec = run_job(job, args, api_key, defaults, 0)
    print(format_result(rec))
    if rec.get("dry_run"):
        print(json.dumps(rec["request"], ensure_ascii=False))
        return 0
    print(ui_line(rec))
    return 0 if rec.get("ok") else 1


def cmd_status(args, defaults: dict) -> int:
    api_key, source = resolve_api_key(args.key, args.key_file)
    queue, outdir = queue_path(args), outdir_path(args)
    jobs = read_jobs(queue, None) if queue.exists() else []
    states = [job_state(job, outdir, i) for i, job in enumerate(jobs)]
    done = [s for s in states if s["exists"]]
    print(f"model     {args.model or defaults.get('model')}")
    print(f"endpoint  {ENDPOINT}")
    print(f"key       {mask_key(api_key)}  来源={source or '未配置'}")
    print(f"queue     {display_path(queue)}  {len(jobs)} 条")
    print(f"outdir    {display_path(outdir)}")
    print(f"runlog    {display_path(runlog_path(args))}")
    if states:
        print(f"已出图     {len(done)}/{len(states)}")
        for st in states[:20]:
            print(f"  {'已出' if st['exists'] else '待出'}  {st['out']}")
    if not api_key:
        print("下一步：python agnes.py --save-key <KEY>", file=sys.stderr)
        return 1
    return 0


def cmd_count(args, defaults: dict) -> int:
    """只报队列待出条数，供 UI 预检，不发网络。"""
    queue = queue_path(args)
    try:
        jobs = read_jobs(queue, args.limit)
    except FileNotFoundError as exc:
        print(f"COUNT 0 {exc}")
        return 1
    outdir = outdir_path(args)
    todo = sum(1 for i, job in enumerate(jobs)
               if args.overwrite or not out_path_for(job, i, outdir).is_file())
    print(f"COUNT {len(jobs)} {todo}")
    return 0


def cmd_sizes(args, defaults: dict) -> int:  # noqa: ARG001
    print("ratio           1K          2K          3K          4K")
    for ratio in RATIOS:
        print(f"{ratio:<6} " + "  ".join(f"{v:>11}" for v in OUTPUT_SIZES[ratio]))
    print(f"\n质量映射：medium/low -> 1K，high -> 2K（Agnes 无 quality 参数）")
    print(f"本项目默认：档位取质量语义，画幅取词库 meta.image_api.ratio（L0 锁死竖构图）")
    return 0


def print_report(args, defaults: dict) -> None:
    api_key, source = resolve_api_key(args.key, args.key_file)
    print(f"endpoint  {ENDPOINT}")
    print(f"model     {args.model or defaults.get('model')}")
    print(f"user-agent {USER_AGENT}")
    print(f"key       {mask_key(api_key)}  来源={source or '未配置'}")
    print(f"timeout   {args.timeout:.0f}s（官方建议 {TIMEOUT_RANGE[0]:.0f}-"
          f"{TIMEOUT_RANGE[1]:.0f}s）  retries={args.retries}")
    print(f"prompts   {display_path(queue_path(args))}")
    print(f"outdir    {display_path(outdir_path(args))}")
    print(f"runlog    {display_path(runlog_path(args))}")


# ---------- 自检（离线，不发网络） ----------
def selftest() -> int:
    defaults = {"model": MODEL, "size": "1K", "ratio": "3:4", "timeout": 300.0}

    class Args:
        size = ratio = quality = None

    args = Args()
    assert resolve_target({"size": "1024x1536", "quality": "high"}, args, defaults)[:2] \
        == ("2K", "3:4"), "定稿 high 应落 2K，画幅跟随 L0 锁死的 3:4"
    assert resolve_target({"size": "1024x1536", "quality": "medium"}, args, defaults)[:2] \
        == ("1K", "3:4"), "抽卡 medium 应落 1K"
    assert resolve_target({"size": "1024x1024", "quality": "medium"}, args, defaults)[1] \
        == "1:1", "方形历史尺寸不该被掰成 3:4"
    assert resolve_target({"size": "1536x1024", "quality": "medium"}, args, defaults)[1] \
        == "3:2", "横版历史尺寸不该被词库竖版接管"
    args.size, args.ratio = "4K", "16:9"
    assert resolve_target({"size": "1024x1536", "quality": "high"}, args, defaults)[:2] \
        == ("4K", "16:9"), "CLI 显式值必须压过队列语义"
    args.size, args.ratio, args.quality = None, None, "high"
    assert resolve_target({"size": "2560x1440", "quality": ""}, args, defaults)[:2] \
        == ("2K", "16:9"), "未知精确尺寸应按面积与宽高比就近折算"
    assert output_size("3:4", "2K") == "1728x2304"

    body = build_body("测试提示词", "2K", "3:4")
    assert body["model"] == MODEL and body["size"] == "2K" and body["ratio"] == "3:4"
    assert body["extra_body"]["response_format"] == "url"
    assert "response_format" not in body, "response_format 禁止出现在顶层"
    assert "tags" not in body, "官方明确不需要 tags"
    assert "quality" not in body, "Agnes 无 quality 参数"
    body = build_body("x", "1K", "1:1", ["https://e.com/a.png"])
    assert body["extra_body"]["image"] == ["https://e.com/a.png"]
    brief = brief_body(build_body("x", "1K", "1:1", ["data:image/png;base64," + "A" * 200]))
    assert len(brief["extra_body"]["image"][0]) < 80, "Data URI 应在打印时被截断"

    assert extract({"data": [{"url": "https://e.com/a.png", "b64_json": ""}]}) \
        == ("url", "https://e.com/a.png")
    assert extract({"data": [{"b64_json": "AAAA"}]}) == ("b64", "AAAA")
    try:
        extract({"data": []})
    except RuntimeError:
        pass
    else:
        raise AssertionError("空响应应报错")

    assert nearest_ratio(1920, 1080) == "16:9"
    assert _clean_ratio("3：4") == "3:4" and _clean_ratio("5:7") == ""
    assert mask_key("sk-1234567890abcd") == "sk-123…abcd（17 位）"
    assert mask_key("") == "<未配置>"

    assert out_path_for({"out": "sub/a.png"}, 0, Path("out")) == Path("out/a.png"), \
        "任务里的目录前缀不该泄漏到输出路径"
    assert out_path_for({"out": ""}, 2, Path("out")).name == "job-3.png"
    assert ui_line({"ok": True, "out": "x.png", "bytes": 10, "elapsed": 1.0,
                    "size": "1K", "ratio": "1:1"}).startswith("UI|OK|x.png|")
    print("selftest OK：尺寸换算 / 请求体 / 响应解析 / 输出路径 / 密钥脱敏 均正常")
    return 0


def main(argv=None) -> int:
    _utf8_console()
    ap = argparse.ArgumentParser(description="Agnes Image 2.5 Flash 出图（任务队列 -> PNG）")
    ap.add_argument("--prompt", help="单条文生图提示词")
    ap.add_argument("--prompt-file", dest="prompt_file", help="从文件读取提示词")
    ap.add_argument("--batch", action="store_true", help="消费任务队列（默认 prompts.jsonl）")
    ap.add_argument("--status", action="store_true", help="密钥/队列/已出图进度")
    ap.add_argument("--count", action="store_true", help="只报队列总数与待出数")
    ap.add_argument("--sizes", action="store_true", help="打印官方尺寸对照表")
    ap.add_argument("--selftest", action="store_true", help="离线自检，不发网络")
    ap.add_argument("--report", action="store_true", help="打印当前生效配置")
    ap.add_argument("--prompts", default=None, help="任务队列路径")
    ap.add_argument("--outdir", default=None,
                    help=f"图片输出目录，相对路径按项目根展开（默认 {DEFAULT_OUTDIR}）")
    ap.add_argument("--runlog", default=None, help="出图日志路径")
    ap.add_argument("--out", default=None, help="单条模式的输出文件名")
    ap.add_argument("--image", action="append", default=[],
                    help="图生图/多图合成输入，可重复；URL 或本地文件（自动转 Data URI）")
    ap.add_argument("--size", default=None, help="强制档位 1K/2K/3K/4K")
    ap.add_argument("--ratio", default=None, help=f"强制宽高比，合法值 {RATIOS}")
    ap.add_argument("--quality", default=None, choices=sorted(QUALITY_TIER),
                    help="无显式档位时按质量映射（medium->1K, high->2K）")
    ap.add_argument("--model", default=None, help=f"默认 {MODEL}")
    ap.add_argument("--key", default=None, help="API Key（不传则读环境变量或密钥文件）")
    ap.add_argument("--key-file", dest="key_file", default=None, help="从这个文件读密钥")
    ap.add_argument("--save-key", dest="save_key", nargs="?", const="-", default=None,
                    help="保存密钥后退出；不带值时从 stdin 读一行")
    ap.add_argument("--limit", type=int, default=None, help="批量模式最多处理几条")
    ap.add_argument("--workers", type=int, default=2, help="并发数，默认 2")
    ap.add_argument("--timeout", type=float, default=None, help="单次请求超时秒数")
    ap.add_argument("--retries", type=int, default=3, help="失败重试次数，默认 3")
    ap.add_argument("--insecure", action="store_true", help="跳过 TLS 证书校验（代理环境用）")
    ap.add_argument("--overwrite", action="store_true", help="已存在的图片也强制重出")
    ap.add_argument("--dry-run", action="store_true", dest="dry_run",
                    help="只构造并打印请求体，不发网络")
    ap.add_argument("--spec", default=None, help="词库路径，读取 meta.image_api 默认值")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()

    defaults = load_defaults(args)
    if args.timeout is None:
        args.timeout = float(defaults.get("timeout") or DEFAULT_TIMEOUT)

    if args.save_key is not None:
        key = args.save_key
        if key == "-":
            key = sys.stdin.readline().strip()
        if not key:
            print("没有拿到密钥", file=sys.stderr)
            return 2
        target = set_api_key(key, project_path(args.key_file) if args.key_file else None)
        print(f"saved -> {display_path(target)}")
        return 0

    if args.sizes:
        return cmd_sizes(args, defaults)
    if args.status:
        return cmd_status(args, defaults)
    if args.count:
        return cmd_count(args, defaults)

    api_key, _source = resolve_api_key(args.key, args.key_file)
    if args.report:
        print_report(args, defaults)
        return 0

    if not args.dry_run and not api_key:
        print("未配置 API Key：设环境变量 AGNES_API_KEY，"
              "或 python agnes.py --save-key <KEY>（写入 agnes.key，已被 gitignore）",
              file=sys.stderr)
        return 2

    if args.batch:
        return cmd_batch(args, api_key, defaults)
    if args.prompt or args.prompt_file or args.out:
        return cmd_single(args, api_key, defaults)
    ap.print_help()
    print("\n提示：单条用 --prompt，批量用 --batch，进度用 --status，自检用 --selftest。",
          file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
