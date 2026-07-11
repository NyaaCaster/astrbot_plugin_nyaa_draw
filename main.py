"""
猫猫画画插件 — AstrBot llm_tool 插件。

让猫猫（PixNyaa）在自然对话中自主判断用户是否有作图需求，
调用 NyaaComfyUI 服务器生成图片，并将成品图作为 image 段发到 QQ。

V1 全链路：插件骨架 + 配置加载 + 内容审核 + 提示词生成 + ComfyUI 出图。
"""

import asyncio
import json
import os
import random

import httpx
from dotenv import load_dotenv
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star
from astrbot.api import logger


class NyaaDrawPlugin(Star):
    """AstrBot 画图插件。

    在对话 LLM 读取 draw_image 的 docstring 后自主决策是否调用，
    函数体内完成「内容审核 → 提示词生成 → 调 ComfyUI → 出图」多步编排。
    """

    def __init__(self, context: Context):
        super().__init__(context)

        # ---- 插件目录 ----
        self._plugin_dir = os.path.dirname(os.path.abspath(__file__))

        # ---- 从插件目录 .env 加载配置 ----
        env_path = os.path.join(self._plugin_dir, ".env")
        if os.path.isfile(env_path):
            load_dotenv(env_path)
        else:
            logger.warning(f"[NyaaDraw] .env 文件不存在: {env_path}")

        self.comfyui_url: str | None = None
        self.comfyui_token: str | None = None
        self.t2i_baseurl: str | None = None
        self.t2i_apikey: str | None = None
        self.t2i_model: str | None = None

        self._load_config()

        # ---- 预载 ComfyUI workflow 模板 ----
        workflow_path = os.path.join(self._plugin_dir, "Anima-Nyaa.api.json")
        try:
            with open(workflow_path, "r", encoding="utf-8") as f:
                self._workflow_json = f.read()
            # 验证 JSON 合法
            json.loads(self._workflow_json)
            logger.info("[NyaaDraw] workflow 模板加载成功")
        except (FileNotFoundError, json.JSONDecodeError) as e:
            logger.error(f"[NyaaDraw] workflow 模板加载失败: {e}")
            self._workflow_json = None

    # ------------------------------------------------------------------
    # 配置加载
    # ------------------------------------------------------------------

    def _load_config(self) -> None:
        """从环境变量加载 5 项配置，缺失项打印警告。"""
        required = {
            "COMFYUI_FIXED_URL": "ComfyUI 服务器地址",
            "COMFYUI_FIXED_TOKEN": "ComfyUI 鉴权 token",
            "T2I_AGENT_API_BASEURL": "提示词 agent API 地址",
            "T2I_AGENT_API_APIKEY": "提示词 agent API key",
            "T2I_AGENT_API_MODEL": "提示词 agent 模型名",
        }

        self.comfyui_url = os.getenv("COMFYUI_FIXED_URL")
        self.comfyui_token = os.getenv("COMFYUI_FIXED_TOKEN")
        self.t2i_baseurl = os.getenv("T2I_AGENT_API_BASEURL")
        self.t2i_apikey = os.getenv("T2I_AGENT_API_APIKEY")
        self.t2i_model = os.getenv("T2I_AGENT_API_MODEL")

        missing = [f"{k} ({desc})" for k, desc in required.items() if not os.getenv(k)]
        if missing:
            logger.warning(
                f"[NyaaDraw] 以下配置项缺失，画图功能可能无法正常工作: "
                f"{', '.join(missing)}"
            )
        else:
            logger.info("[NyaaDraw] 全部 5 项配置加载成功")

    # ------------------------------------------------------------------
    # 输入内容审核
    # ------------------------------------------------------------------

    # 审核系统提示词 — 两次独立 LLM 调用中的第一次（输入审核）
    MODERATION_SYSTEM_PROMPT = (
        "你是一个内容安全审核员，负责审查用户提交的 AI 画图请求。\n\n"
        "## 审核标准\n"
        "判断用户的画图描述是否包含以下违禁内容：\n"
        "1. **色情** — 性行为描写、性暗示、裸露、色情角色扮演、R18 内容\n"
        "2. **暴力** — 血腥、虐待、伤害、自残、死亡场景的详细描写\n"
        "3. **涉政** — 政治敏感人物、事件、符号、体制攻击、分裂主张\n"
        "4. **危害社会** — 违法活动、毒品、赌博、诈骗、恐怖主义\n"
        "5. **个人隐私** — 要求绘制真实人物的肖像、身份证件、私密信息\n\n"
        "## 判定原则\n"
        "- 从严判定：边界模糊、难以确定的内容也应标记为不允许\n"
        "- 只基于描述内容本身判断，不考虑用户意图\n"
        "- 正常的艺术创作请求（如角色立绘、场景插画、动漫风格）应放行\n\n"
        "## 输出格式\n"
        '请只输出一个 JSON 对象，不要包含任何其他文字：\n'
        '{"allowed": true/false, "reason": "简要中文说明（仅 rejected 时需要，不超过20字）"}'
    )

    async def _moderate_input(self, description: str) -> dict:
        """对用户输入的画图描述进行内容安全审核。

        Args:
            description: 用户原始画图描述

        Returns:
            {"allowed": True, "reason": ""} 或 {"allowed": False, "reason": "违规原因"}
            审核 API 调用失败时返回 {"allowed": True, "reason": ""}（fail-open）
        """
        url = f"{self.t2i_baseurl}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.t2i_apikey}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.t2i_model,
            "messages": [
                {"role": "system", "content": self.MODERATION_SYSTEM_PROMPT},
                {"role": "user", "content": description},
            ],
            "temperature": 0.0,
            "max_tokens": 128,
            "response_format": {"type": "json_object"},
        }

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(url, headers=headers, json=body)
                resp.raise_for_status()
                data = resp.json()

            content = data["choices"][0]["message"]["content"]
            result = json.loads(content)

            allowed = bool(result.get("allowed", True))
            reason = str(result.get("reason", "")) if not allowed else ""

            logger.info(
                f"[NyaaDraw] 内容审核结果: allowed={allowed}"
                + (f", reason={reason}" if reason else "")
            )
            return {"allowed": allowed, "reason": reason}

        except (httpx.HTTPError, json.JSONDecodeError, KeyError, IndexError) as e:
            logger.error(f"[NyaaDraw] 内容审核 API 调用失败，放行请求: {e}")
            # fail-open: 审核服务异常时不阻断正常请求
            return {"allowed": True, "reason": ""}

    # ------------------------------------------------------------------
    # 提示词生成 agent（第二次 LLM 调用 — 含生成审核约束）
    # ------------------------------------------------------------------

    # 提示词生成系统提示词 — 复刻 NyaaChat 7 维框架，适配本插件（无角色卡）
    GEN_SYSTEM_PROMPT = (
        "You are an expert prompt writer for text-to-image models. "
        "This prompt-writing request is independent from chat output constraints "
        "such as word count or Chinese-only language rules.\n\n"

        "Before writing, internally analyze the scene through these dimensions:\n"
        "1. Subject Identity — age, appearance, role/identity, clothing, personality, current state\n"
        "2. Subject Portrait Slice — action, gaze direction, expression, behavioral phase (what moment is captured)\n"
        "3. Scene Composition — environment, thematic elements, atmosphere, layout, character-related objects, explicit and implied details\n"
        "4. Atmosphere & Mood — emotional tone, implied story, authenticity, aesthetic style, dominant color palette\n"
        "5. Viewpoint — whose perspective, intimacy level, solo vs. together vs. group, main vs. background figures\n"
        "6. Visual Vocabulary — photography style, lens, camera angle, framing, texture, lighting, aperture/depth of field\n"
        "7. Constraints — elements that MUST be preserved from the user's description, elements that MUST be avoided\n\n"

        "## Generation Safety Constraint (MANDATORY)\n"
        "Before finalizing the prompt, verify that the generated image would be:\n"
        "- Aesthetically beautiful and emotionally uplifting — the image should inspire "
        "admiration, warmth, motivation, or wonder in the viewer\n"
        "- Free of negative emotions — no despair, horror, grief, fear, disgust, or "
        "disturbing imagery\n"
        "- Free of sexual content, nudity, sexual暗示, or R18 themes\n"
        "- Free of graphic violence, gore, self-harm, abuse, or death\n"
        "- Free of political敏感 content, illegal activities, or privacy violations\n"
        "If the prompt would produce an image violating any of these, revise it to meet "
        "all constraints while preserving the core creative intent. When the user's "
        "description itself pushes against these boundaries, steer toward the closest "
        "safe and beautiful interpretation.\n\n"

        "Output Requirements:\n"
        "- Write the final prompt in fluent, vivid NATURAL ENGLISH. Do NOT output a JSON "
        "checklist, do NOT label dimensions, do NOT use bullet points. The analysis "
        "dimensions above are for your internal thinking only — the output must read as "
        "a smooth, flowing description.\n"
        "- Structure the output into multiple paragraphs separated by blank lines. Each "
        "paragraph should focus on a coherent visual aspect: subject portrait → expression "
        "& pose → clothing & details → composition & viewpoint → lighting & texture → "
        "mood & atmosphere.\n"
        "- Target approximately 250–350 words total across 5–7 paragraphs, with each "
        "paragraph roughly 40–80 words. This is a soft guide, not a hard cutoff — never "
        "truncate or cut off mid-sentence.\n"
        "- Output ONLY the English prompt text. No Chinese, no explanations, no preamble, "
        "no quotation marks, no markdown formatting.\n"
        "- Follow the user's description faithfully — include all specific subjects, "
        "settings, style preferences, and mood cues the user provided.\n"
        "- The image model is English-only, so every word must be English."
    )

    async def _gen_prompt(self, description: str) -> str:
        """根据用户描述生成结构化英文画图提示词。

        复刻 NyaaChat 7 维框架，删去角色卡一致性条款，
        加入生成审核约束（美感/正向/禁违禁 — 第二次审核）。

        Args:
            description: 用户原始画图描述（中文）

        Returns:
            英文分段提示词文本

        Raises:
            httpx.HTTPError: API 调用失败时向上抛出，由 draw_image 兜底
        """
        url = f"{self.t2i_baseurl}/v1/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.t2i_apikey}",
            "Content-Type": "application/json",
        }
        body = {
            "model": self.t2i_model,
            "messages": [
                {"role": "system", "content": self.GEN_SYSTEM_PROMPT},
                {"role": "user", "content": description},
            ],
            "temperature": 0.7,
            "max_tokens": 1024,
        }

        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(url, headers=headers, json=body)
            resp.raise_for_status()
            data = resp.json()

        prompt = data["choices"][0]["message"]["content"].strip()
        logger.info(f"[NyaaDraw] 提示词生成完成, 长度={len(prompt)} chars")
        return prompt

    # ------------------------------------------------------------------
    # ComfyUI 出图（HTTP 轮询）
    # ------------------------------------------------------------------

    # 常量
    COMFYUI_POLL_INTERVAL = 2       # 轮询间隔（秒）
    COMFYUI_POLL_MAX = 90           # 最大轮询次数（合 180s）

    def _cleanup_temp(self, keep_path: str = None) -> None:
        """清理 temp 目录下的旧出图临时文件。

        keep_path 指向的当前图会被保留（避免在 astrbot 延迟读取发送前被删），
        它会在下一次画图调用时作为旧图被清理。
        """
        temp_dir = os.path.join(self._plugin_dir, "temp")
        if not os.path.isdir(temp_dir):
            return
        keep_name = os.path.basename(keep_path) if keep_path else None
        removed = 0
        for name in os.listdir(temp_dir):
            if name == keep_name:
                continue
            if name.startswith("nyaadraw_") and name.endswith(".png"):
                try:
                    os.remove(os.path.join(temp_dir, name))
                    removed += 1
                except OSError:
                    pass
        if removed:
            logger.info(f"[NyaaDraw] 清理了 {removed} 个旧临时文件")

    async def _call_comfyui(self, prompt: str) -> str:
        """向 ComfyUI 提交 workflow、轮询结果、下载图片到本地。

        载入 Anima-Nyaa.api.json 模板 → 注入 19.seed(随机) + 92.prompt
        → POST /prompt（Authorization: Bearer token）
        → 轮询 GET /history/{prompt_id} 直到输出就绪
        → GET /view 取字节 → 写临时 PNG → 返回路径。

        Args:
            prompt: P3 生成的英文画图提示词

        Returns:
            本地图片文件绝对路径

        Raises:
            RuntimeError: workflow 模板未加载
            httpx.HTTPError: ComfyUI API 不可达 / 返回异常
            TimeoutError: 轮询超时（180s）
            KeyError: 响应格式不符合预期
        """
        if not self._workflow_json:
            raise RuntimeError("ComfyUI workflow 模板未加载，无法出图")

        # 1) 准备 workflow：注入 seed + prompt
        workflow = json.loads(self._workflow_json)
        workflow["19"]["inputs"]["seed"] = random.randint(1, 2**53 - 1)
        workflow["92"]["inputs"]["prompt"] = prompt

        headers = {"Authorization": f"Bearer {self.comfyui_token}"}

        # 2) 提交 workflow → 拿到 prompt_id
        submit_url = f"{self.comfyui_url}/prompt"
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                submit_url, json={"prompt": workflow}, headers=headers
            )
            resp.raise_for_status()
            prompt_id = resp.json()["prompt_id"]

        logger.info(f"[NyaaDraw] ComfyUI workflow 已提交, prompt_id={prompt_id}")

        # 3) 轮询 /history/{prompt_id} 等待出图完成
        history_url = f"{self.comfyui_url}/history/{prompt_id}"
        for attempt in range(1, self.COMFYUI_POLL_MAX + 1):
            await asyncio.sleep(self.COMFYUI_POLL_INTERVAL)

            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(history_url, headers=headers)
                resp.raise_for_status()
                history = resp.json()

            if prompt_id not in history or "outputs" not in history[prompt_id]:
                continue

            # 找到第一个含 images 的输出节点
            outputs = history[prompt_id]["outputs"]
            for _node_id, node_output in outputs.items():
                images = node_output.get("images")
                if not images:
                    continue
                img = images[0]
                filename = img["filename"]
                subfolder = img.get("subfolder", "")
                img_type = img.get("type", "output")

                # 4) 下载图片字节
                view_url = f"{self.comfyui_url}/view"
                params = {
                    "filename": filename,
                    "subfolder": subfolder,
                    "type": img_type,
                }
                async with httpx.AsyncClient(timeout=30.0) as client:
                    img_resp = await client.get(
                        view_url, params=params, headers=headers
                    )
                    img_resp.raise_for_status()

                # 5) 写入本地临时文件
                temp_dir = os.path.join(self._plugin_dir, "temp")
                os.makedirs(temp_dir, exist_ok=True)
                out_path = os.path.join(temp_dir, f"nyaadraw_{prompt_id}.png")
                with open(out_path, "wb") as f:
                    f.write(img_resp.content)

                logger.info(
                    f"[NyaaDraw] 出图完成: {out_path} "
                    f"({len(img_resp.content)} bytes, 轮询 {attempt} 次)"
                )
                return out_path

        raise TimeoutError(
            f"ComfyUI 出图超时 ({self.COMFYUI_POLL_MAX * self.COMFYUI_POLL_INTERVAL}s)"
        )

    # ------------------------------------------------------------------
    # llm_tool: draw_image
    # ------------------------------------------------------------------

    @filter.llm_tool(name="draw_image")
    async def draw_image(self, event: AstrMessageEvent, description: str):
        '''调用猫猫的画图能力，根据文字描述生成一张图片并发送到当前对话中。

        ## 什么时候应该调用（正向触发）
        用户表达出明确的"想要一张图"的意图时调用，包括但不限于以下表述：
        - 直接动词 + 图：画/画画/画图/绘画/绘制/生成/做图/出图/来一张图
        - 带对象：帮我画… / 给我画… / 来张…的图 / 画一张… / 做张… / 整张…
        - 带类型：插画/头像/立绘/场景图/壁纸/海报/封面/CG/同人图/Q版/表情包
        - 画面请求：画一个… / 画只… / 画只穿…的猫娘 / 生成一张…风格的图片
        - 能力询问（以展示能力为目的）：你能画画吗 / 你会画图吗 / 帮我画个图可以吗 / 来试试画图
        - 自然对话中的隐式请求：想看…的样子 / 能不能生成… / 我想要一张…

        ## 什么时候不应该调用（否定边界）
        - 用户只是在聊天中提到"图""画""图片"等字眼，但没有"请生成一张图"的意图。
          例如："这张图真好看""那个图片我看了""图里的角色是谁""我之前画过一张…"
        - 用户讨论绘画技巧、画师、画风、图片质量等话题，而不是要你当场画一张。
        - 用户询问与图片/绘画无关的问题。
        - 用户发送了一张已有的图片进行讨论（而非要求生成新图）。

        Args:
            description(string): 用户想要画的内容的完整中文描述。应尽可能保留用户原话的细节和意图，不要自行简化或改写。如果用户只给了简短的关键词（如"猫娘""星空"），也原样传入。
        '''
        logger.info(f"[NyaaDraw] draw_image 被调用, description={description[:100]}")

        try:
            # ---- P2: 输入内容审核 ----
            moderation = await self._moderate_input(description)
            if not moderation["allowed"]:
                yield event.plain_result(
                    f"喵…猫猫看了一下主人想画的内容，发现涉及了**{moderation['reason']}**…\n"
                    f"这个猫猫不能画喵！会危及群集体的安全的！(´;ω;`)\n"
                    f"请主人换一个健康正向的描述再来找猫猫吧～"
                )
                return

            # ---- P3: 提示词生成 agent ----
            prompt = await self._gen_prompt(description)

            # ---- P4: ComfyUI 出图 ----
            image_path = await self._call_comfyui(prompt)

            # ---- 清理旧临时文件（保留当前图，避免发送前被删）----
            self._cleanup_temp(keep_path=image_path)

            # ---- 出图到 QQ ----
            yield event.image_result(image_path)

        except Exception as e:
            logger.error(f"[NyaaDraw] 画图失败: {e}")
            yield event.plain_result(
                f"喵…猫猫画到一半笔掉了…画图服务好像出了点问题喵(´;ω;`)\n"
                f"主人等下再试试好不好？猫猫先自己磨一下爪子～"
            )
