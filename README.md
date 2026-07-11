# astrbot_plugin_nyaa_draw

> Nyaa猫猫画画 —— 对话 LLM 自主决策触发，经两道内容审核后调 NyaaComfyUI 出图，成品图直接发送到 QQ 聊天。

让猫猫（PixNyaa）在自然对话中**自主判断**用户是否有作图需求，无需斜杠命令。由 bot 的对话 LLM 读取工具描述后自行决定是否调用，函数体内完成「内容审核 → 提示词生成 → ComfyUI 出图」多步编排，最后将成品图作为 image 段发到 QQ。

## 兼容性

- 需要 AstrBot `>=4.0.0`。
- 需要 `httpx`、`python-dotenv`（`requirements.txt` 已声明）。
- 依赖外部服务：**NyaaComfyUI**（自建 ComfyUI 服务器）和 **DeepSeek API**（提示词 agent + 内容审核）。

## 设计理念

本插件**不是**一个普通的斜杠命令画图机器人，它是一个 **llm_tool 决策型插件**：

- ❌ 不要求用户记住或输入任何命令；
- ❌ 不暴露技术错误码——所有异常都以猫娘语态兜底回复；
- ❌ 不绕过内容安全——每次画图前经过两道独立 LLM 审核；
- ✅ 由对话 LLM 在自然对话中自行判断"用户现在是不是想要一张图"；
- ✅ 画图流程全自动：审核 → 生成英文提示词 → ComfyUI 出图 → QQ 发图。

换句话说：**群友在聊天里自然说"帮我画个 XXX"，猫猫就会画出来**，完全不需要 "/画图" 这类命令。

## 安装

本插件暂未发布至 AstrBot 插件市场，需手动安装：

1. 将本仓库克隆至 AstrBot 的 `data/plugins/` 目录中：
   ```bash
   git clone https://github.com/NyaaCaster/astrbot_plugin_nyaa_draw.git \
     /root/DockerContainer/DockerRes/astrbot/data/plugins/astrbot_plugin_nyaa_draw/
   ```
2. 放置 `.env` 配置文件到插件目录（见下方配置项）。
3. 重启 AstrBot（`docker restart astrbot` 或重建容器）。
4. 进入 WebUI「插件管理」确认插件已启用、`draw_image` 工具已注册。

## 配置项

所有配置通过插件目录下的 `.env` 文件提供（**不入 Git，需手动放置**）：

| 配置 | 说明 |
| --- | --- |
| `COMFYUI_FIXED_URL` | NyaaComfyUI 服务器地址（含端口） |
| `COMFYUI_FIXED_TOKEN` | ComfyUI 鉴权 Bearer token（含 `$` 字符，dotenv 单引号包裹） |
| `T2I_AGENT_API_BASEURL` | 提示词 agent / 审核 API 地址（如 `https://api.deepseek.com`） |
| `T2I_AGENT_API_APIKEY` | 提示词 agent / 审核 API key |
| `T2I_AGENT_API_MODEL` | 提示词 agent / 审核模型名（如 `deepseek-v4-flash`） |

`.env` 示例（值均为示意，请替换为实际配置）：

```ini
COMFYUI_FIXED_URL=http://111.198.54.18:58199
COMFYUI_FIXED_TOKEN='$2b$12$...'
T2I_AGENT_API_BASEURL=https://api.deepseek.com
T2I_AGENT_API_APIKEY=sk-...
T2I_AGENT_API_MODEL=deepseek-v4-flash
```

> 🔴 **安全红线**：`.env` 已被 `.gitignore` 排除，**禁止**将密钥提交到 Git 仓库。`COMFYUI_FIXED_TOKEN` 含 `$` 字符，必须用单引号包裹以避免 shell 展开。

## 工作原理

### 触发决策

```
用户发言 → AstrBot 将「对话上下文 + draw_image 工具描述」
  → 交给对话 LLM → 模型自主判断是否需要画图
  → 若是 → function call draw_image(description)
```

决策质量取决于两件事：**工具 docstring 的精调程度** 和 **对话模型的能力**。docstring 已覆盖正向触发词（画/生成/来张/帮我画…）和否定边界（纯讨论/评价已有图片时不触发）。

### 全链路流程

```
draw_image(description)
  ├─ P2 输入内容审核
  │    └─ deepseek 判 5 类违禁（色情/暴力/涉政/危害社会/隐私）
  │       命中 → 猫娘语态拒绝文本 + return
  │       通过 → 继续
  ├─ P3 提示词生成 agent
  │    └─ deepseek 7 维框架（含生成审核约束：美感/正向/禁违禁）
  │       → 分段纯英文提示词（~300 words, 5-7 段）
  └─ P4 ComfyUI 出图
       ├─ 载入 Anima-Nyaa.api.json workflow 模板
       ├─ 注入 19.seed（随机）+ 92.prompt（英文提示词）
       ├─ POST /prompt → 轮询 /history → /view 取字节
       └─ 落本地 temp/ → event.image_result(path) → QQ 出图
```

### 异常处理

任一环节失败（API 不可达、ComfyUI 超时、响应异常等）→ **不暴露技术错误码**，统一以猫娘语态兜底回复。输入审核 API 故障时 fail-open（放行），不阻断正常画图。

## 注意事项

- **工具调用超时**：AstrBot 默认 `tool_call_timeout=120s`，ComfyUI 轮询上限为 180s。若出图排队较长可能触发框架超时，可评估调大 AstrBot 的 `tool_call_timeout` 配置。
- **图片压缩**：AstrBot 默认开启图片压缩（max 1280 / q95），出图画质会有所降低。如需原画质，可调整 AstrBot 的 `image_compress_enabled` 配置。
- **QQ 图片发送**：出图落地为本地文件后通过 `event.image_result(path)` 发送，避开了 NapCat 拉不到外链的风险。
- **临时文件**：出图缓存在插件目录 `temp/` 下，每次画图前自动清理旧文件，保留当前图避免竞态删除。
- **并发画图**：依赖 ComfyUI 自带队列处理，插件本身不做并发控制。多用户同时触发时按 ComfyUI 队列顺序出图。

## License

MIT
