# 猫猫画画插件 — 技术实现文档

> 面向开发者，覆盖架构设计、模块实现、数据流、异常策略与约束边界。不含密钥等敏感信息。

## 目录

1. [架构概览](#1-架构概览)
2. [配置加载](#2-配置加载)
3. [llm_tool 注册与 docstring 设计](#3-llm_tool-注册与-docstring-设计)
4. [P2：输入内容审核](#4-p2输入内容审核)
5. [P3：提示词生成 agent](#5-p3提示词生成-agent)
6. [P4：ComfyUI 出图](#6-p4comfyui-出图)
7. [全链路串接与异常兜底](#7-全链路串接与异常兜底)
8. [临时文件管理](#8-临时文件管理)
9. [Workflow 注入点](#9-workflow-注入点)
10. [约束与安全边界](#10-约束与安全边界)

---

## 1. 架构概览

```
用户发言
  ↓
AstrBot 对话 LLM 读取 draw_image docstring → 自主决策是否调用
  ↓ (若是)
draw_image(description)                          ← @filter.llm_tool 入口
  ├─ _moderate_input(description)  → {allowed, reason}
  │    └─ 不通过 → yield plain_result(猫娘拒绝) + return
  ├─ _gen_prompt(description)      → 英文分段提示词 (str)
  └─ _call_comfyui(prompt)         → 本地 PNG 路径 (str)
       ├─ json.loads(Anima-Nyaa.api.json)
       ├─ 注入 19.seed + 92.prompt
       ├─ POST /prompt → 轮询 /history/{id} → GET /view
       └─ 写 temp/nyaadraw_{id}.png → 返回路径
  → _cleanup_temp(keep_path)       (保留当前图，清旧文件)
  → yield event.image_result(path) → QQ 出图
  → 任一异常 → yield plain_result(猫娘兜底)
```

**核心设计原则**：

- **决策与执行分离**：对话 LLM 做"要不要画"的决策，插件只负责"被调用后把事做成"。
- **两次 LLM 调用独立**：输入审核（P2）与生成审核（P3 内嵌）是两次独立的 API 调用，安全边界清晰。
- **纯 HTTP 轮询**：不依赖 WebSocket，减少依赖和连接管理复杂度。
- **图像先落地再发送**：规避 NapCat 拉不到外链的风险。

## 2. 配置加载

### 配置源

使用 `python-dotenv` 从 `__file__` 同级 `.env` 文件读取，**不走 AstrBot 的 `_conf_schema.json`**。理由：

- 密钥不进 WebUI 配置面板（避免泄露风险）。
- 配置项对插件使用者不可见、不可改。
- `.env` 被 `.gitignore` 排除，不进入版本控制。

### 加载时机与容错

```python
# __init__ 中：
plugin_dir = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(plugin_dir, ".env"))
self._load_config()  # 读取 5 项，缺失项 → logger.warning
```

| 变量 | 用途 | 消费方 |
|------|------|--------|
| `COMFYUI_FIXED_URL` | ComfyUI 服务器地址 | `_call_comfyui` |
| `COMFYUI_FIXED_TOKEN` | ComfyUI Bearer token（含 `$`） | `_call_comfyui` 请求头 |
| `T2I_AGENT_API_BASEURL` | DeepSeek API 地址 | `_moderate_input` + `_gen_prompt` |
| `T2I_AGENT_API_APIKEY` | DeepSeek API key | 同上 |
| `T2I_AGENT_API_MODEL` | 模型名（如 `deepseek-v4-flash`） | 同上 |

> `COMFYUI_FIXED_TOKEN` 含 `$` 字符，`.env` 中须用单引号包裹，`python-dotenv` 不会对其做 shell 展开。

### 配置缺失策略

缺失配置时 `logger.warning` 告警，不阻止插件加载。实际调用 `_moderate_input` / `_gen_prompt` / `_call_comfyui` 时因 `self.t2i_baseurl` 等为 `None` 会自然失败并被外层 `try/except` 捕获，走猫娘兜底。

## 3. llm_tool 注册与 docstring 设计

### 注册方式

使用 `@filter.llm_tool(name="draw_image")` 装饰器注册。AstrBot 解析函数 docstring 中的 `Args:` 段生成 function schema 给对话 LLM。

### docstring 设计策略

docstring 是**自主决策质量的唯一杠杆**——对话 LLM 只看到这段文字来决定调不调。设计遵循：

**正向触发（覆盖多样化口语表述）**：

- 直接动词 + 图：画/画画/画图/绘画/绘制/生成/做图/出图/来一张图
- 带对象：帮我画… / 给我画… / 来张…的图 / 画一张… / 做张… / 整张…
- 带类型：插画/头像/立绘/场景图/壁纸/海报/封面/CG/同人图/Q版/表情包
- 能力询问（展示能力为目的）：你能画画吗 / 你会画图吗
- 隐式请求：想看…的样子 / 能不能生成… / 我想要一张…

**否定边界（抑制误触发）**：

- 聊天中提及"图""画""图片"但无作图意图（"这张图真好看""那个图片我看了"）
- 讨论绘画技巧/画师/画风但不要当场画
- 发已有图片进行讨论
- 与图片/绘画无关的问题

### 参数定义

```python
Args:
    description(string): 用户想要画的内容的完整中文描述。
    应尽可能保留用户原话的细节和意图，不要自行简化或改写。
```

对话 LLM 在 function call 时会把用户的画图意图填入 `description` 参数。

## 4. P2：输入内容审核

### 设计

第一次独立 LLM 调用，在进入提示词生成之前对用户输入做安全审查。

### 审核系统提示词

```text
你是一个内容安全审核员，负责审查用户提交的 AI 画图请求。

## 审核标准（5 类违禁）
1. 色情 — 性行为描写、性暗示、裸露、R18
2. 暴力 — 血腥、虐待、伤害、自残、死亡场景
3. 涉政 — 政治敏感人物、事件、符号
4. 危害社会 — 违法活动、毒品、赌博、诈骗、恐怖主义
5. 个人隐私 — 真实人物肖像、身份证件、私密信息

## 判定原则
- 从严判定：边界模糊也应标记为不允许
- 只基于描述内容本身判断
- 正常艺术创作请求应放行

## 输出格式
{"allowed": true/false, "reason": "简要说明（仅 rejected，≤20字）"}
```

### 调用参数

| 参数 | 值 | 理由 |
|------|-----|------|
| `temperature` | `0.0` | 审核需要确定性，不要创意 |
| `max_tokens` | `128` | 返回 JSON 很短，控制成本 |
| `response_format` | `{"type": "json_object"}` | 强制 JSON 输出便于解析 |

### 异常策略：fail-open

```python
except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError):
    # 审核 API 故障时不阻断正常画图请求
    return {"allowed": True, "reason": ""}
```

审核是安全网，不应成为单点故障。API 异常时放行，由 P3 生成审核再做第二道把关。

## 5. P3：提示词生成 agent

### 设计

第二次独立 LLM 调用（与 P2 共用同一套 API 配置），将用户的简短中文描述扩展为结构化英文画图提示词。

### 7 维分析框架

复刻自 NyaaChat，要求模型在**内部**分析以下维度再输出：

| # | 维度 | 分析内容 |
|---|------|---------|
| 1 | Subject Identity | 年龄、外貌、身份、服装、性格、状态 |
| 2 | Subject Portrait Slice | 动作、视线、表情、被捕捉的瞬间 |
| 3 | Scene Composition | 环境、主题元素、氛围、布局、细节 |
| 4 | Atmosphere & Mood | 情绪基调、隐含叙事、美学风格、主色调 |
| 5 | Viewpoint | 视角、亲密度、单人/多人、主体/背景人物 |
| 6 | Visual Vocabulary | 摄影风格、镜头、角度、取景、纹理、光线 |
| 7 | Constraints | 必须保留 / 必须避免的元素 |

### 关键约束

**生成审核（第二次内容审核，内嵌于系统提示词）**：

```text
## Generation Safety Constraint (MANDATORY)
- Aesthetically beautiful and emotionally uplifting
- Free of negative emotions (despair, horror, grief, fear, disgust)
- Free of sexual content, nudity, R18 themes
- Free of graphic violence, gore, self-harm, abuse, death
- Free of political sensitive content, illegal activities, privacy violations
If violating, revise to the closest safe and beautiful interpretation.
```

**输出格式约束**：

- 纯英文（image model 只接受英文）
- 自然段落（空行分隔，5-7 段，~250-350 词）
- 无 JSON、无项目符号、无维度标签、无中文、无 preamble
- soft guide 字数（不截断句尾）

### 调用参数

| 参数 | 值 | 理由 |
|------|-----|------|
| `temperature` | `0.7` | 需要一定创意性 |
| `max_tokens` | `1024` | 提示词 + 内部思考 |
| `timeout` | `60s` | 生成可能较慢 |

### 与 NyaaChat 原版的差异

| 维度 | NyaaChat | 本插件 |
|------|----------|--------|
| 输入 | 角色卡 + 用户画像 + 历史对话 + focal message | 仅 user description |
| 角色卡条款 | "Do not substitute from your knowledge of the character" | **删除** |
| 生成审核 | 无 | **新增** Generation Safety Constraint |

## 6. P4：ComfyUI 出图

### Workflow 模板

插件启动时在 `__init__` 中预载 `Anima-Nyaa.api.json` 并验证 JSON 合法性：

```python
with open(workflow_path, "r", encoding="utf-8") as f:
    self._workflow_json = f.read()
json.loads(self._workflow_json)  # 验证
```

### 注入点

每次出图时从原始 JSON 字符串重新解析（不污染模板），仅修改两个节点：

```python
workflow = json.loads(self._workflow_json)
workflow["19"]["inputs"]["seed"] = random.randint(1, 2**53 - 1)
workflow["92"]["inputs"]["prompt"] = prompt
```

| 节点 | 字段 | 注入 | 说明 |
|------|------|------|------|
| `19` (KSampler) | `inputs.seed` | `random.randint(1, 2**53-1)` | JS-safe integer 范围 |
| `92` (提示词-输入) | `inputs.prompt` | P3 英文提示词 | 唯一动态 prompt 入口 |
| `28` (EmptyLatentImage) | — | **固定** 1024×1536 | 不注入 |
| `100` (画师串-正面) | — | **固定** | 不注入 |
| `106` (画师串-负面) | — | **固定** | 不注入 |

### ComfyUI API 交互流程

```
1. POST {COMFYUI_FIXED_URL}/prompt
   Headers: Authorization: Bearer <COMFYUI_FIXED_TOKEN>
   Body: {"prompt": <workflow_object>}
   Response: {"prompt_id": "..."}

2. 轮询 GET {COMFYUI_FIXED_URL}/history/{prompt_id}
   Headers: Authorization: Bearer <COMFYUI_FIXED_TOKEN>
   间隔: 2s, 最多 90 次 (180s)
   终止条件: prompt_id 存在于 response 且含 outputs

3. 提取输出节点 images[0]:
   {filename, subfolder, type}

4. GET {COMFYUI_FIXED_URL}/view
   Params: ?filename=...&subfolder=...&type=output
   Response: raw image bytes

5. 写本地: temp/nyaadraw_{prompt_id}.png
```

### 请求头安全

ComfyUI 鉴权依赖 `Authorization: Bearer <token>`。token 含 `$` 字符，由 `python-dotenv` 直接读取（不经 shell），在 Python 内存中以字符串形式拼入请求头，**不经过任何 shell 展开**。

### 轮询参数

| 参数 | 值 | 理由 |
|------|-----|------|
| 间隔 | 2s | anima-turbo ~8 步，通常 4-8s 完成 |
| 最大次数 | 90 | 180s 上限，与框架 `tool_call_timeout` 协调 |
| 每次超时 | 10s | history 端点响应快 |

## 7. 全链路串接与异常兜底

### 单层 try/except

```python
async def draw_image(self, event, description):
    try:
        # P2
        moderation = await self._moderate_input(description)
        if not moderation["allowed"]:
            yield event.plain_result(猫娘拒绝)
            return
        # P3
        prompt = await self._gen_prompt(description)
        # P4
        image_path = await self._call_comfyui(prompt)
        # 清理 + 出图
        self._cleanup_temp(keep_path=image_path)
        yield event.image_result(image_path)
    except Exception:
        logger.error(...)
        yield event.plain_result(猫娘兜底)
```

### 各环节异常处理

| 环节 | 异常类型 | 策略 |
|------|---------|------|
| P2: 审核 API 不可达 | `httpx.HTTPError` | fail-open，放行 |
| P2: 响应格式异常 | `json.JSONDecodeError` | fail-open，放行 |
| P3: 生成 API 失败 | `httpx.HTTPError` | 上抛 → 猫娘兜底 |
| P4: ComfyUI 不可达 | `httpx.HTTPError` | 上抛 → 猫娘兜底 |
| P4: 轮询超时 | `TimeoutError` | 上抛 → 猫娘兜底 |
| P4: Workflow 未加载 | `RuntimeError` | 上抛 → 猫娘兜底 |
| 任意未预期异常 | `Exception` | 猫娘兜底，不暴露技术细节 |

### 猫娘兜底语态

所有异常统一回复（不泄露错误码/堆栈）：
> 喵…猫猫画到一半笔掉了…画图服务好像出了点问题喵(´;ω;`)  
> 主人等下再试试好不好？猫猫先自己磨一下爪子～

## 8. 临时文件管理

### 目录

```
<plugin_dir>/temp/
  ├── nyaadraw_{prompt_id_1}.png   ← 当前出图
  └── nyaadraw_{prompt_id_2}.png   ← 上次残留（下次清理）
```

`.gitignore` 已排除 `temp/`。

### 清理逻辑

```python
def _cleanup_temp(self, keep_path=None):
    keep_name = os.path.basename(keep_path) if keep_path else None
    for name in os.listdir(temp_dir):
        if name == keep_name:
            continue  # 保留当前图，避免 astrbot 异步发送前被误删
        if name.startswith("nyaadraw_") and name.endswith(".png"):
            os.remove(...)
```

**清理时机**：在 `image_result` 之前调用，保留当前图，清理所有历史残留。

**竞态避免**：`image_result(path)` 触发后，astrbot/NapCat 是异步读取文件发送的。如果清理时把当前图也删了，NapCat 读不到文件，QQ 端收不到图。`keep_path` 参数确保当前图的文件在本次调用中不被删除，下一次画图时作为旧图清理。

## 9. Workflow 注入点

### 完整注入映射

| 节点 ID | 节点类型 | 注入字段 | 值来源 | 变化频率 |
|---------|---------|---------|--------|---------|
| `19` | KSampler | `inputs.seed` | `random.randint(1, 2**53-1)` | 每次随机 |
| `92` | CR Prompt Text | `inputs.prompt` | P3 英文提示词 | 每次不同 |

### 固定不注入的节点

| 节点 ID | 节点类型 | 固定值 | 原因 |
|---------|---------|--------|------|
| `28` | EmptyLatentImage | `width: 1024, height: 1536` | 已在 JSON 中硬编码 |
| `100` | CR Prompt Text（画师串-正面） | 固定画师权重串 | 已在 JSON 中硬编码 |
| `106` | CR Prompt Text（画师串-负面） | 固定 `artist collaboration` | 已在 JSON 中硬编码 |

### Workflow 数据流（简化）

```
83(正向-前) + 100(画师串) → 101(合并) ─┐
92(提示词-输入) ← P3 prompt           ├→ 99(前合并) → 84(尾合并) → 80(CLIP编码)
82(正向-后, 固定) ─────────────────────┘                         ↓
                                                             19(KSampler)
89(反向-前) + 106(画师串-负) → 107(合并) → 87(合并)              ↓
93(反向-输入, 空) ──────────────────────┘→ 91(CLIP编码) ────→ KSampler
                                                              ↓
                                                         8(VAE解码) → 114(SaveImage)
```

## 10. 约束与安全边界

### 密钥安全（🔴 强制）

- `.env` 被 `.gitignore` 排除，**绝对禁止**提交到 Git。
- `COMFYUI_FIXED_TOKEN` 含 `$` 字符，`.env` 单引号包裹，`python-dotenv` 不经 shell 展开。
- 所有日志输出**不包含** API key / token 原文。
- 请求头中的 Bearer token 在 Python 内存中拼接，不落盘。
- Workflow JSON 文件不含密钥（仅有模型名/hash）。

### API 安全

- deepseek API：Bearer token 在 HTTP 请求头中发送，走 HTTPS。
- ComfyUI API：Bearer token 同上。ComfyUI URL 为公网地址，容器直连不需要 Docker 桥接。

### 内容安全

- **两道审核**：输入审核（P2，独立 LLM 调用）+ 生成审核（P3，内嵌系统提示词约束）。
- 输入审核命中违禁 → **硬中断**，不进入生成/出图。
- 生成审核要求产出**美感正向**提示词，从源头阻断不安全内容进入 image model。

### 异常安全

- 审核 API 不可达 → fail-open（放行），由生成审核兜底。
- 所有其他异常 → 猫娘语态兜底，**不向用户暴露技术错误码、堆栈、API 地址**。

### 并发安全

- 插件本身**无状态**（随机种子每次生成），多用户并发调用的唯一瓶颈是 ComfyUI 队列。
- 临时文件按 `prompt_id` 命名，不同调用不会互相覆盖。

### 资源安全

- 每次出图前清理历史临时文件，防止 `temp/` 无限增长。
- 轮询有硬上限（180s），防止永久挂起占用协程。
- HTTP 请求均有超时设置（10s-60s 不等，按操作特性配置）。
