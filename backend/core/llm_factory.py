# backend/core/llm_factory.py
# LLM Factory：统一封装大模型调用，按 Agent 类型路由。
# 规矩：所有 Agent 必须通过此模块获取模型，禁止直接调用 init_chat_model。

from typing import Type, Any  # 类型注解用：Type 表示「某个类本身」，Any 表示任意类型
from pydantic import BaseModel  # 结构化输出的 Schema 都是它的子类
import httpx  # HTTP 客户端库（用来自定义网络行为）
from langchain.chat_models import init_chat_model  # 2.3 学的：创建聊天模型（1.x 写法）
from langchain_core.language_models import BaseChatModel  # 聊天模型的基类（类型注解用）
from langchain_core.runnables import Runnable  # 「可运行对象」基类，结构化模型属于它

from backend.config import get_settings  # 读配置（API Key、base_url 等）
from backend.core.logger import get_logger  # 结构化日志

logger = get_logger(__name__)  # 本模块的日志器，name 用当前模块名

# ── 自定义 httpx 客户端：绕过系统代理 ───────────────────────────
# 背景：Windows 系统代理或 HTTPS_PROXY 环境变量会被 httpx 默认探测到，
#       导致 DeepSeek 请求经代理后 TLS 握手失败。DeepSeek 国内可直连，无需代理。
# trust_env=False 表示：完全忽略系统代理和相关环境变量。
_HTTP_ASYNC_CLIENT = httpx.AsyncClient(  # 异步客户端（给 ainvoke/astream 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),  # 总超时 120 秒，建立连接超时 15 秒
)
_HTTP_SYNC_CLIENT = httpx.Client(  # 同步客户端（给 invoke 用）
    trust_env=False,
    timeout=httpx.Timeout(120.0, connect=15.0),
)
# ── Agent 类型 → 模型标识符 的路由表 ────────────────────────────
# 想给某类业务换模型(对应模型厂商)，只改这里一行即可。
# 这里的_AGENT_MODEL_ROUTING键对应的是业务名称，值是每个业务选择的哪个模型厂商
_AGENT_MODEL_ROUTING: dict[str, str] = {
    "qa":               "deepseek-llm",  # 智能问答
    "pcb":              "deepseek-llm",  # PCB封装图生成
    "step":             "deepseek-llm",  # STEP数模图生成
    "drawing_extract":  "qwen-version",  # 二维工程图视觉理解 / Golden Set
    "symbol":           "deepseek-llm",
    # 符号 Agent 的视觉通道要读引脚图（"哪个脚在哪条边上"），必须走多模态模型。
    "symbol_vision":    "qwen-version",  # OrCAD符号图生成
    "intent":           "deepseek-llm",  # 意图识别
    "summarize":        "deepseek-llm",  # 对话摘要压缩
}

# 模型厂商 → 接入配置：模型名 / API Key / base_url 分别从 Settings 的哪个字段取
# 换模型：改 .env.local 即可；换厂商：改 _AGENT_MODEL_ROUTING 指向的厂商 key
_MODEL_PROVIDER_MAP: dict[str, dict[str, str]] = {
    "deepseek-llm": {
        "model_field": "deepseek_model_chat",   # .env.local → DEEPSEEK_MODEL_CHAT
        "api_key_field": "deepseek_api_key",    # .env.local → DEEPSEEK_API_KEY
        "base_url_field": "deepseek_base_url",  # .env.local → DEEPSEEK_BASE_URL
    },
    "qwen-version": {
        "model_field": "qwen_model_vl",         # .env.local → QWEN_MODEL_VL
        "api_key_field": "qwen_api_key",        # .env.local → QWEN_API_KEY
        "base_url_field": "qwen_base_url",      # .env.local → QWEN_BASE_URL
    },
}

# 模型厂商 → 结构化输出方式：
# function_calling 依赖模型返回 tool_call（DeepSeek 支持）；
# json_mode 走 response_format={"type":"json_object"}（阿里兼容接口支持，VL 模型更稳）
_STRUCTURED_METHOD_MAP: dict[str, str] = {
    "deepseek-llm": "function_calling",
    "qwen-version": "json_mode",
}

class LLMFactory:
    """大模型工厂（统一获取模型的唯一入口）。
    用 @classmethod 定义方法，意味着不用创建对象、直接用 LLMFactory.get_llm(...) 调用。

    用法：
        llm = LLMFactory.get_llm("qa")                                       # 普通模型
        structured = LLMFactory.get_structured_llm("drawing_extract", 某Schema)  # 结构化输出模型
        response = await llm.ainvoke(messages)
    """

    _instances: dict[str, BaseChatModel] = {}  # 类变量：模型实例缓存（缓存键 → 模型），全类共享

    # 类方法
    @classmethod
    def _get_settings(cls):
        """内部小工具：取配置对象。"""
        return get_settings()

    @classmethod
    def _build_model_kwargs(cls, model_key: str) -> dict[str, Any]:
        """内部方法：按模型厂商组装 init_chat_model 需要的所有参数（两厂商均走 OpenAI 兼容接口）。"""
        settings = cls._get_settings()  # 取配置
        provider = _MODEL_PROVIDER_MAP[model_key]  # 该厂商的接入配置

        model_id = getattr(settings, provider["model_field"])     # 实际模型名，来自 .env.local
        api_key = getattr(settings, provider["api_key_field"])    # 该厂商自己的 API Key
        base_url = getattr(settings, provider["base_url_field"])  # 该厂商自己的接口地址
        if not api_key or not base_url:
            raise ValueError(f"模型厂商 '{model_key}' 的 api_key/base_url 未配置，请检查 .env.local")

        return {
            "model": model_id,                           # 模型名，如 "deepseek-flash" / "qwen3.8-flash"
            "model_provider": "openai",                  # 强制走 langchain-openai（两厂商均兼容 OpenAI 接口）
            "temperature": 0,                            # 默认 0：评分/批改要稳定输出
            "api_key": api_key,                          # 该厂商自己的 Key
            "base_url": base_url,                        # 该厂商自己的接口地址
            "max_retries": 0,                            # 模型层不重试；重试统一由 retry.py（3.5）管
            "http_async_client": _HTTP_ASYNC_CLIENT,     # 用上面绕过代理的异步客户端
            "http_client": _HTTP_SYNC_CLIENT,            # 同步客户端
        }

    @classmethod
    def get_llm(
            cls,
            agent_type: str,  # Agent 类型，必须在路由表里
            temperature: float = 0,  # 温度：对话类可传 0.3~0.7，评分类保持 0
            streaming: bool = False,  # 是否流式输出（问答/面试对话用）
    ) -> BaseChatModel:
        """按 Agent 类型获取模型实例（带缓存）。
        相同 (模型, 温度, 是否流式) 的组合只会创建一次，之后复用。"""
        if agent_type not in _AGENT_MODEL_ROUTING:  # 校验：不认识的类型直接报错（早暴露问题）
            raise ValueError(
                f"未知 agent_type: '{agent_type}'，"
                f"可用类型：{list(_AGENT_MODEL_ROUTING.keys())}"
            )
        # 模型智能体厂商
        model_key = _AGENT_MODEL_ROUTING[agent_type]  # 查路由表，拿到模型标识符(模型厂商)
        # print(f'model_key: {model_key}')
        # 用「模型_温度_是否流式」拼一个缓存键：不同组合各缓存一份
        cache_key = f"{model_key}_{temperature}_{streaming}"
        # print(f'cache_key: {cache_key}')
        if cache_key not in cls._instances:  # 缓存里没有才新建
            # print(f'cache_key: {cache_key}')
            kwargs = cls._build_model_kwargs(model_key)  # 组装基础参数
            # print(f'kwargs: {kwargs}')
            kwargs["temperature"] = temperature  # 覆盖温度
            kwargs["streaming"] = streaming  # 设置是否流式
            if model_key == "deepseek-llm":
                kwargs["extra_body"] = {
                    "thinking": {"type": "disabled"}}  # DeepSeek 推理模型（如 deepseek-v4-pro）可强制关闭思考模式
            # print(f'kwargs: {kwargs}')
            # 关键字传参
            llm = init_chat_model(**kwargs)  # 真正创建模型（** 表示把字典展开成关键字参数）
            cls._instances[cache_key] = llm  # 存进缓存
            logger.info(  # 记一条结构化日志，便于观察
                "llm_factory.model_initialized",
                agent_type=agent_type, model_key=model_key,
                temperature=temperature, streaming=streaming,
            )
        return cls._instances[cache_key]

    @classmethod
    def get_structured_llm(
            cls,  # self
            agent_type: str,
            output_schema: Type[BaseModel],  # 期望的输出结构（一个 Pydantic 模型类）  Type[类] 传进来的是一个类而不是对象
            temperature: float = 0,
    ) -> Runnable:
        """获取「绑定了结构化输出 Schema」的模型。
        调用它的 ainvoke 后，直接返回一个 output_schema 类型的对象（不是文本）。
        注意：json_mode（Qwen）要求 prompt 里必须包含 "json" 字样，否则阿里接口报 400。"""
        llm = cls.get_llm(agent_type, temperature=temperature)  # 先拿普通模型
        model_key = _AGENT_MODEL_ROUTING[agent_type]  # 查路由表拿厂商 key
        method = _STRUCTURED_METHOD_MAP.get(model_key, "function_calling")  # 按厂商选结构化方式
        # 绑定 Pydantic 结构；method 由厂商能力决定（见 _STRUCTURED_METHOD_MAP）
        return llm.with_structured_output(output_schema, method=method)

    @classmethod
    def clear_cache(cls) -> None:
        """清空模型实例缓存（测试时用）。"""
        cls._instances.clear()
        logger.info("llm_factory.cache_cleared")


# ── 模块级便捷函数（Agent 代码里的推荐写法）────────────────────────
# 比写 LLMFactory.get_llm(...) 更简洁，直接 from llm_factory import get_llm 即可。

def get_llm(agent_type: str, temperature: float = 0, streaming: bool = False) -> BaseChatModel:
    """LLMFactory.get_llm 的便捷入口。"""
    return LLMFactory.get_llm(agent_type, temperature=temperature, streaming=streaming)


def get_structured_llm(agent_type: str, output_schema: Type[BaseModel]) -> Runnable:
    """LLMFactory.get_structured_llm 的便捷入口。"""
    return LLMFactory.get_structured_llm(agent_type, output_schema)


if __name__ == '__main__':
    import asyncio
    from pathlib import Path
    from langchain_core.messages import HumanMessage, SystemMessage
    from pydantic import BaseModel, Field

    async def main():
        # 测试自然语言模型
        # llm = LLMFactory.get_llm("qa")  # 通过工厂拿模型
        # resp = await llm.ainvoke([HumanMessage(content="用一句话介绍 Python")])
        # print("DeepSeek 回复:", resp.text)  # .text 取文本

        # 测试结构化输出 视觉模型
        class PackageInfo(BaseModel):
            package_name: str = Field(description="封装名称")
            pin_count: int = Field(description="引脚数量")
            body_size: str = Field(description="封装体尺寸，如 6x6mm")
            pitch: str = Field(description="引脚间距，如 0.5mm")

        llm_struct = LLMFactory.get_structured_llm("drawing_extract", PackageInfo)

        img_path = Path(r"D:\WorkSpace\Xien\project_01\sample\picture\ADC_图纸10.png")
        import base64
        img_data = base64.b64encode(img_path.read_bytes()).decode()

        messages = [
            # 注意：阿里 json_mode 要求消息里必须出现 "json" 字样，否则报 invalid_parameter_error
            SystemMessage(content="你是一个电子元器件封装信息提取专家，请从图片中准确提取封装参数，"
                                  "并严格按给定的 json 格式输出（key 必须用英文，不要输出 markdown 代码块）。"),
            HumanMessage(content=[
                {"type": "text", "text": (
                    "请从这张封装图纸中提取封装名称、引脚数、封装体尺寸和引脚间距，"
                    "结果以 json 返回，格式必须为：\n"
                    '{"package_name": "封装名称", "pin_count": 36, "body_size": "6x6mm", "pitch": "0.5mm"}'
                )},
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_data}"}}
            ])
        ]
        result = await llm_struct.ainvoke(messages)
        print(f"结构化结果: {result.model_dump()}")

    asyncio.run(main())
