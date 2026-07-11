"""
猫猫画画插件 — AstrBot llm_tool 插件。

让猫猫（PixNyaa）在自然对话中自主判断用户是否有作图需求，
调用 NyaaComfyUI 服务器生成图片，并将成品图作为 image 段发到 QQ。

V1 阶段：插件骨架 + 配置加载 + llm_tool 注册（stub）。
"""

import os

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

        # ---- 从插件目录 .env 加载配置 ----
        plugin_dir = os.path.dirname(os.path.abspath(__file__))
        env_path = os.path.join(plugin_dir, ".env")
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
        # P1 stub: 仅确认工具被正确调用，后续 P2-P4 逐步接入实际逻辑
        logger.info(f"[NyaaDraw] draw_image 被调用, description={description[:100]}")
        yield event.plain_result(
            f"喵~猫猫收到了主人的画图请求！(P1 stub)\n"
            f"主人想画的是：「{description}」\n"
            f"不过画笔还在快递路上，等后面的 P2-P4 阶段猫猫就能真的画出图来啦～"
        )
