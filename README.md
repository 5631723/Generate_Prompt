# 写真生图提示词抽卡台 v1

基于词库采样的「写真提示词」生成工具：从经过预审的词库（`slots.json`）中按风格包随机采样槽位值，拼装成中文自然语言提示词，再交给 Agnes 出图客户端（`agnes.py`）消费任务队列、生成图片。

分工：**采样交给代码，措辞交给规范，出图交给 `agnes.py`**。三者通过任务队列单向通信，各管一段、互不侵入。

## 文件构成

| 文件 | 角色 | 读者 |
|---|---|---|
| `写真生图提示词-v1.md` | 规范与指令：架构、措辞规则、拼装骨架、渲染示例 | 人、LLM（编译器备用路径） |
| `slots.json` | 词库数据（唯一事实源）：槽位值、tags、黑名单、拼装模板、`meta.image_api` 出图参数 | 代码（采样器 / 出图） |
| `sampler.py` | 采样逻辑：seeded 随机、黑名单过滤、跨槽校验、去重、拼装 | Python 解释器 |
| `agnes.py` | Agnes Image 2.5 Flash 出图客户端：任务队列 → 本地图片 + 出图日志 | Python 解释器 |
| `ui.py` | tkinter 桌面图形界面：抽卡 + 出图一体 | 使用者 |
| `codex-config/AGENTS.md` | Codex CLI 的角色提示词（项目维护约定） | Codex CLI |
| `启动抽卡台.bat` | Windows 启动脚本（双击即开界面） | 使用者 |

维护规则：**改词库只动 `slots.json`；改采样逻辑只动 `sampler.py`；改出图逻辑只动 `agnes.py`；规范文档必须与代码保持同步**。

## 架构

- **L0 锁死层**：主体、身形、镜头（85mm f/1.8）、画幅（竖构图 3:4）、纯净度等固定不变，任何输入（含用户输入）不得修改。
- **L1 采样层**：`sampler.py` 采样——先抽风格包，包内 4 槽（scene / outfit / pose / light）联动保证协调；再从独立池抽 3 槽（hair / gaze / anchor）制造差异。
- **L2 用户覆盖层**：7 个可覆盖槽位 `hair / gaze / anchor / scene / outfit / pose / light`，覆盖值原样透传；撞黑名单 / requires 时明确报错，不静默重抽。
- **出图层**：`agnes.py` 消费任务队列（`prompts.jsonl`），按 `slots.json` 的 `meta.image_api` 定档位与画幅，出图落盘 `images/`，记账写 `runlog.jsonl`。

理论组合空间：6 包 × 3⁴ 包内 × 6 发型 × 5 神情 × 6 吸睛点 ≈ 87480 种（受黑名单、跨槽校验与去重约束，实际略少）。

## 环境要求

- Python 3.10+（代码使用 `str | None` 等新语法）
- 出图需要 Agnes AI 平台的 API Key（见下节）

## 快速开始

### 1. 配置 API Key（出图前必须）

`agnes.key` 是**密钥文件，已被 `.gitignore` 忽略，不会随仓库分发**。从 GitHub 克隆本项目后，需要自己配置一次：

1. 在 [Agnes AI 平台](https://agnes-ai.com) 申请 API Key（形如 `sk-...`）。
2. 任选一种方式配置（优先级从高到低）：

| 优先级 | 方式 | 说明 |
|---|---|---|
| 1 | `python agnes.py --key sk-xxx --status` | 命令行临时传入（不回显明文） |
| 2 | 环境变量 `AGNES_API_KEY` | 设置后全局生效 |
| 3 | `--key-file <路径>` | 指定密钥文件 |
| 4 | 环境变量 `AGNES_API_KEY_FILE=<路径>` | 指定密钥文件 |
| 5 | 项目根 `agnes.key` 或 `.agnes.key` | 默认搜索位置，推荐 |

3. **最省事的配置命令**（写入项目根 `agnes.key`，权限收紧为 600）：

```
python agnes.py --save-key sk-你的密钥
```

4. 验证配置：

```
python agnes.py --status
```

只会显示脱敏后的密钥（如 `sk-123…abcd（17 位）`），不会回显明文。未配置时会提示下一步操作。

密钥文件格式：**首个非注释行即密钥**，支持 `#` 注释行、BOM 与行尾空白。注意：密钥一旦公开即失效，请勿把 `agnes.key` 内容或明文密钥贴进仓库、Issue 或聊天记录。

### 2. 图形界面（推荐）

双击 `启动抽卡台.bat`，或运行：

```
python ui.py
```

界面上点「Agnes 出图 → 设置密钥」可直接把密钥写入 `agnes.key`。

### 3. 命令行抽卡

```bash
python sampler.py --n 5                        # 抽 5 条，写入任务队列
python sampler.py --n 3 --pack cafe            # 限定风格包
python sampler.py --n 3 --draw-seed 20260908   # 固定种子，可复现
python sampler.py --n 3 --set anchor=指尖与杯沿的接触点   # L2 覆盖
python sampler.py --n 3 --quality high         # 覆盖出图质量
```

### 4. 命令行出图

```bash
python agnes.py --batch                        # 消费整个任务队列，出图到 images/
python agnes.py --batch --limit 2 --dry-run    # 只打印请求体，不发网络
python agnes.py --batch --size 2K --ratio 3:4  # 强制档位与画幅
python agnes.py --prompt "..." --out a.png     # 单条文生图
python agnes.py --prompt "..." --image ref.png # 图生图（本地文件自动转 Data URI）
python agnes.py --status                       # 密钥 / 队列 / 已出图进度
python agnes.py --selftest                     # 离线自检，不发网络
```

## sampler.py CLI

| 参数 | 说明 |
|---|---|
| `--n` | 抽取条数，默认 5 |
| `--pack` | 限定风格包，可重复传（默认全部包轮转） |
| `--draw-seed` | 采样种子，留空随机；随 variant_id 记录，可复现 |
| `--set KEY=VALUE` | L2 用户覆盖，key ∈ hair / gaze / anchor / scene / outfit / pose / light |
| `--quality` | 覆盖出图质量（默认 medium，定稿 high） |
| `--size` | 覆盖出图画幅（默认 1024x1536） |
| `--spec` | 词库路径，相对路径按项目根解析（默认 `slots.json`） |
| `--prompts` / `--manifest` | 自定义产物路径，相对路径按项目根解析（与 cwd 无关） |

## agnes.py CLI

| 参数 | 说明 |
|---|---|
| `--batch` | 消费任务队列（默认 `tmp/imagegen/prompts.jsonl`） |
| `--limit` | 最多处理几条 |
| `--prompt` / `--prompt-file` | 单条模式 |
| `--image` | 图生图输入，可重复（URL 或本地文件） |
| `--size` | 强制档位 `1K / 2K / 3K / 4K` |
| `--ratio` | 强制宽高比（1:1 / 3:4 / 4:3 / 16:9 / 9:16 / 2:3 / 3:2 / 21:9） |
| `--quality` | 无显式档位时按质量映射（medium/low → 1K，high → 2K） |
| `--workers` | 并发数，默认 2 |
| `--timeout` | 单次请求超时秒数（默认 300，官方建议 60–360） |
| `--retries` | 失败重试次数，默认 3（带抖动退避，4xx 不重试） |
| `--overwrite` | 已存在的图片也强制重出 |
| `--dry-run` | 只构造并打印请求体，不发网络 |
| `--status` / `--count` / `--sizes` / `--report` | 状态 / 待出数 / 尺寸对照表 / 生效配置 |
| `--save-key` | 保存密钥到项目根 `agnes.key` 后退出 |
| `--key` / `--key-file` | 临时指定密钥（不回显明文） |
| `--insecure` | 跳过 TLS 证书校验（代理环境用） |

## 产物

| 产物 | 路径（相对项目根） | 说明 |
|---|---|---|
| 任务队列 | `tmp/imagegen/prompts.jsonl` | 抽卡产出，`{prompt, size, quality, out}`，出图消费后覆盖写入 |
| 记录表 | `output/imagegen/gacha/manifest.jsonl` | 抽卡全量历史 + 去重依据（追加写入） |
| 出图日志 | `output/imagegen/gacha/runlog.jsonl` | 每张图的档位 / 画幅 / 耗时 / 结果（追加写入） |
| 图片 | `images/` | 出图落盘目录 |

产物目录（`tmp/`、`output/`）与密钥文件都在 `.gitignore` 里，仓库只跟踪源码与规范。

## 环境变量

优先级：**CLI 显式传参 > 环境变量 > 项目根下默认值**。代码不写死绝对路径，`ui.py` 复用 `sampler.py` / `agnes.py` 的解析函数，两边读同一套配置。

| 变量 | 作用 | 默认值（相对项目根） |
|---|---|---|
| `GACHA_SPEC` | 词库位置 | `slots.json` |
| `GACHA_PROMPTS` | 任务队列位置 | `tmp/imagegen/prompts.jsonl` |
| `GACHA_MANIFEST` | 记录表位置 | `output/imagegen/gacha/manifest.jsonl` |
| `AGNES_API_KEY` | API Key | — |
| `AGNES_API_KEY_FILE` | 密钥文件路径 | `agnes.key` / `.agnes.key` |
| `AGNES_PROMPTS` / `AGNES_OUTDIR` / `AGNES_RUNLOG` | 出图队列 / 输出目录 / 日志 | 见上表 |
| `AGNES_SIZE` / `AGNES_RATIO` | 强制档位 / 画幅 | — |

## UI 功能（ui.py）

- **采样分组**：风格包、条数（1–20）、draw_seed、质量、多行槽位覆盖（可动态增删）。
- **Agnes 出图分组**：输出档位（1K–4K）、宽高比、并发数、密钥状态（脱敏显示）、「设置密钥」按钮、「开始出图 / 停止出图」。
- **结果区**：抽卡结果表格（变体ID / 字数 / 包 / 服装 / 状态）、提示词预览、一键复制、**定稿重抽(high)**（以 high 语义写入任务队列）。
- **出图日志区**：实时出图进度与结果，可拖拽调整高度。
- 「刷新记录」「打开 manifest / 任务队列目录」。
- 自检模式：`python ui.py --selftest`（验证输出解析）、`python ui.py --smoke`（构建窗口后即销毁）。

## 核心特性

- **风格包**：city / home / night / retro / cafe / athleisure 共 6 包，包内四槽联动保证协调，包数量保证多样性。
- **黑名单过滤**：对会显著抬升 API 拒率的组合（如 sleepwear + reclined、night 包 thin_strap + 车灯高光）直接跳过；**新增词条必须过黑名单 + requires 校验再入库**。
- **跨槽一致性**：anchor 带 `requires`（如「指尖与杯沿」需要「杯」）时，校验场景/服装/姿态/光线文本 + 细节描述 `d` 中是否出现该道具；不满足则重抽，用户覆盖场景时直接报错。
- **全量去重**：按 manifest 全量 hash（sha1 前 6 位）去重，组合相同即重抽。
- **可复现**：draw_seed 记录进 variant_id，同 seed 完整复现同一批组合。
- **长度告警**：提示词长度超出 `slots.json` 的 `meta.char_range`（当前 200–270 字）时在记录中标记 warn。
- **出图稳健**：带抖动的退避重试、4xx 不重试（避免白烧配额）、`.part` 原子落盘、图片内容校验、已存在跳过（`--overwrite` 强制重出）、并发可控。
- **明确报错**：用户覆盖撞黑名单 / requires 时输出冲突槽位与原因，不静默「空间耗尽」。

## EXE 打包

`sampler.py` / `agnes.py` / `ui.py` 支持 PyInstaller 打包：打包后 `PROJECT_ROOT` 指向 EXE 所在目录，产物（`tmp/` `output/` `images/`）落在 EXE 旁、用户可见可备份；词库优先读 EXE 旁的 `slots.json`（可直接改词库而无需重新打包），缺失时才回退内置副本。`ui.py` 在 EXE 模式下改为同进程调用采样与出图逻辑，不依赖外部 Python。

## 换模型时的改动点

- 换 SD / Flux：需英文、拆出 `negative_prompt`、长度压到 77 token 级别。
- 换 Midjourney：需 `--ar 3:4 --style raw` 等参数后缀，去掉镜头长描述。
- 换其他图像 API：改 `agnes.py` 的 `ENDPOINT` / `MODEL` / 请求体构造，并同步更新 `slots.json` 的 `meta.image_api`。
- L0 锁死层的语义约束不变，只改语法外壳。
