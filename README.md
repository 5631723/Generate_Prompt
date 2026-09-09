# 写真生图提示词抽卡台 v1

基于词库采样的「写真提示词」生成工具：从经过预审的词库（`slots.json`）中按风格包随机采样槽位值，拼装成可直接送进生图模型（gpt-image-2）的中文自然语言提示词。

分工：**采样交给代码，措辞交给规范，出图交给生图调用**。采样器只负责采样与拼装、不调用任何生图接口，保证组合协调、可复现、可去重。

## 文件构成

| 文件 | 角色 | 读者 |
|---|---|---|
| `写真生图提示词-v1.md` | 规范与指令：架构、措辞规则、拼装骨架、渲染示例 | 人、LLM（编译器备用路径） |
| `slots.json` | 词库数据（唯一事实源）：槽位值、tags、黑名单、拼装模板 | 代码（采样器） |
| `sampler.py` | 采样逻辑：seeded 随机、黑名单过滤、跨槽校验、去重、拼装 | Python 解释器 |
| `ui.py` | tkinter 桌面图形界面，包装 sampler.py 的 CLI | 使用者 |
| `启动抽卡台.bat` | Windows 启动脚本（双击即开界面） | 使用者 |

维护规则：**改词库只动 `slots.json`；改逻辑只动 `sampler.py`；规范文档必须与两者保持同步**。

## 架构：L0 / L1 / L2 三层

- **L0 锁死层**：主体、身形、镜头（85mm f/1.8）、画幅（竖构图 3:4）、纯净度等固定不变，任何输入（含用户输入）不得修改。
- **L1 采样层**：`sampler.py` 采样——先抽风格包，包内 4 槽（scene / outfit / pose / light）联动保证协调；再从独立池抽 3 槽（hair / gaze / anchor）制造差异。
- **L2 用户覆盖层**：7 个可覆盖槽位 `hair / gaze / anchor / scene / outfit / pose / light`，覆盖值原样透传；撞黑名单 / requires 时明确报错，不静默重抽。

理论组合空间：6 包 × 3⁴ 包内 × 6 发型 × 5 神情 × 6 吸睛点 ≈ 87480 种（受黑名单、跨槽校验与去重约束，实际略少）。

## 快速开始

### 图形界面（推荐）

双击 `启动抽卡台.bat`，或运行：

```
python ui.py
```

### 命令行

```bash
python sampler.py --n 5                        # 抽 5 条
python sampler.py --n 3 --pack cafe            # 限定风格包
python sampler.py --n 3 --draw-seed 20260908   # 固定种子，可复现
python sampler.py --n 3 --set anchor=指尖与杯沿的接触点   # L2 覆盖
python sampler.py --n 3 --quality high         # 覆盖出图质量
```

### CLI 参数

| 参数 | 说明 |
|---|---|
| `--n` | 抽取条数，默认 5 |
| `--pack` | 限定风格包，可重复传（默认全部包轮转） |
| `--draw-seed` | 采样种子，留空随机；随 variant_id 记录，可复现 |
| `--set KEY=VALUE` | L2 用户覆盖，key ∈ hair / gaze / anchor / scene / outfit / pose / light |
| `--quality` | 覆盖出图质量（默认 medium，定稿 high） |
| `--size` | 覆盖出图画幅（默认 1024x1536） |
| `--prompts` / `--manifest` | 自定义产物路径 |

## 产物

- **任务队列** `tmp/imagegen/prompts.jsonl`（默认位于项目上级目录）：`{prompt, size, quality, out}`，供生图脚本消费（`image_gen.py` 未随仓库提供）。
- **记录表** `output/imagegen/gacha/manifest.jsonl`（默认位于项目上级目录）：`{variant_id, draw_seed, hash, chars, warn, slots, prompt}`，既是全量历史，也是去重依据。

## 核心特性

- **风格包**：city / home / night / retro / cafe / athleisure 共 6 包，包内四槽联动保证协调，包数量保证多样性。
- **黑名单过滤**：对会显著抬升 API 拒率的组合（如 sleepwear + reclined、night 包 thin_strap + 车灯高光）直接跳过；**新增词条必须过黑名单 + requires 校验再入库**。
- **跨槽一致性**：anchor 带 `requires`（如「指尖与杯沿」需要「杯」）时，校验场景/服装/姿态/光线文本 + 细节描述 `d` 中是否出现该道具；不满足则重抽，用户覆盖场景时直接报错。
- **全量去重**：按 manifest 全量 hash（sha1 前 6 位）去重，组合相同即重抽，而非只看最近若干条。
- **可复现**：draw_seed 记录进 variant_id，同 seed 完整复现同一批组合。
- **长度告警**：提示词长度超出 `slots.json` 的 `meta.char_range`（当前 200–270 字）时在记录中标记 warn。
- **明确报错**：用户覆盖撞黑名单 / requires 时输出冲突槽位与原因，不静默「空间耗尽」。

## UI 功能（ui.py）

- 左侧参数面板：风格包、条数（1–20）、draw_seed、质量、多行槽位覆盖（可动态增删）。
- 右侧结果区：结果表格（变体ID / 字数 / 包 / 服装 / 状态）、提示词预览、一键复制。
- **定稿重抽(high)**：把选中组合以 quality=high 直接写入任务队列（绕过去重，交由生图环节消费）。
- 「刷新记录」「打开 manifest / 任务队列目录」。
- 自检模式：`python ui.py --selftest`（验证输出解析逻辑）、`python ui.py --smoke`（构建窗口后即销毁）。

## 设计约束

- 采样器只采样与拼装，**不调用任何生图接口**；出图对接外部 `image_gen.py`（未提供）。
- 措辞规则（详见 `写真生图提示词-v1.md`）：全部正向描述、信息顺序固定、禁止抽象营销词、女性魅力只走剪裁/面料/身体轮廓/光线/眼神五个通道、画面纯净无文字水印。
- 提示词为中文自然语言，适用于 gpt-image-2（无 negative_prompt 参数）。

## 换模型时的改动点

- 换 SD / Flux：需英文、拆出 `negative_prompt`、长度压到 77 token 级别。
- 换 Midjourney：需 `--ar 3:4 --style raw` 等参数后缀，去掉镜头长描述。
- L0 锁死层的语义约束不变，只改语法外壳；同步更新 `slots.json` 的 `meta.model`。
