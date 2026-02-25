import json
import logging
import httpx
from typing import Any, Dict, List, Optional
from openai import AsyncOpenAI
from app.core.config import settings
from app.models.schemas import Message, ChatResponse, ToolCall, ToolCallFunction

logger = logging.getLogger(__name__)

class ModelRouter:
    def __init__(self):
        # 初始化火山引擎客户端 (直连，trust_env=False 防止宿主机代理变量干扰)
        self.volcengine_client = AsyncOpenAI(
            api_key=settings.volcengine_api_key,
            base_url=settings.volcengine_base_url,
            http_client=httpx.AsyncClient(trust_env=False),
        )

        # Gemini 客户端：强制通过内置 Clash 代理(settings.proxy_url = http://clash:7890)
        # trust_env=False：防止宿主机 HTTP_PROXY/HTTPS_PROXY 污染路由
        self.gemini_client = AsyncOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            http_client=httpx.AsyncClient(
                proxy=settings.proxy_url,
                trust_env=False,
            ),
        )

        # 维护可用模型列表（最后更新：2026-02-23）
        self.available_models = {
            # 火山引擎 Coding Plan 模型
            # base_url: https://ark.cn-beijing.volces.com/api/coding/v3（订阅）
            #           https://ark.cn-beijing.volces.com/api/v3（按量）
            "volcengine": [
                "ark-code-latest",               # Coding Plan 统一路由，自动选最优模型
                "doubao-seed-code-preview-251028",  # 豆包编程旗舰（Agentic Coding 优化）
                "doubao-seed-1-8-251228",           # 豆包 Seed 1.8 通用
                "glm-4-7-251222",                   # GLM-4.7
                "deepseek-v3-2-251201",             # DeepSeek V3.2
                "kimi-k2-thinking-251104",          # Kimi K2 thinking
            ],
            "gemini": [
                # ── Gemini 3 系列（2025-12 ~ 2026-02，预览版） ──
                "gemini-3.1-pro-preview",        # 最新，2026-02-19 发布，强力 Agent/Coding
                "gemini-3-flash-preview",         # Frontier 级多模态，2025-12 发布
                # ── Gemini 2.5 系列（稳定 GA） ──
                "gemini-2.5-pro",                 # 最先进推理+编码，GA
                "gemini-2.5-flash",               # 高性价比，GA，✅ 已验证可用
                "gemini-2.5-flash-lite",          # 最快最廉价，GA
                # ── Gemini 2.0 系列（已标为 deprecated，将于 2026-03-31 关闭） ──
                "gemini-2.0-flash",               # ⚠️ deprecated，迁移至 2.5-flash
                "gemini-2.0-flash-lite",          # ⚠️ deprecated，迁移至 2.5-flash-lite
                # ── Gemini 1.x 系列已全部下线（返回 404），不再列出 ──
            ]
        }

        # 默认模型
        self.default_model = "ark-code-latest"

    def _select_model(self, requested_model: Optional[str]) -> tuple[str, str]:
        """
        根据请求选择合适的模型和提供商。
        如果用户指定了模型，则使用指定的；否则使用默认的。
        """
        if requested_model:
            for provider, models in self.available_models.items():
                if requested_model in models:
                    return provider, requested_model
            # 如果找不到，默认回退到火山引擎的指定模型（假设用户输入的是火山引擎的自定义模型名）
            logger.warning(f"Model {requested_model} not found in registry, defaulting to volcengine provider.")
            return "volcengine", requested_model
        
        # 默认路由逻辑：选择性价比最高或能力最强的模型
        return "volcengine", self.default_model

    @staticmethod
    def _format_messages(messages: List[Message]) -> List[Dict[str, Any]]:
        """
        将 Message 对象列表转换为 OpenAI API 所需的 dict 格式。

        支持四种 role 的字段拼装：
        - system / user — 标准格式 {role, content}
        - assistant — 含 tool_calls 时需将工具调用列表序列化进去
        - tool    — 需包含 tool_call_id 和 content（执行结果 JSON 字符串）
        """
        formatted: List[Dict[str, Any]] = []
        for msg in messages:
            d: Dict[str, Any] = {"role": msg.role}

            # content
            if msg.content is not None:
                d["content"] = msg.content
            else:
                # OpenAI 要求 content 不能缺少，为 null 时传空字符串
                d["content"] = ""

            # assistant 角色携带 tool_calls
            if msg.role == "assistant" and msg.tool_calls:
                d["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": tc.type,
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in msg.tool_calls
                ]

            # tool 角色携带 tool_call_id
            if msg.role == "tool" and msg.tool_call_id:
                d["tool_call_id"] = msg.tool_call_id

            formatted.append(d)
        return formatted

    async def generate(
        self,
        messages: List[Message],
        model: Optional[str] = None,
        temperature: float = 0.7,
        max_tokens: int = 2048,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> ChatResponse:
        """
        统一生成入口。

        Args:
            messages:    对话消息列表，支持 role=tool 的工具消息。
            model:       指定模型，为 None 时使用默认模型。
            temperature: 生成温度。
            max_tokens:  最大 token 数。
            tools:       OpenAI tool definitions 列表；为 None 或空列表时不启用工具调用。
            tool_choice: 工具选择策略，默认 'auto'。
        """
        provider, selected_model = self._select_model(model)

        logger.info(f"Routing request to provider: {provider}, model: {selected_model}")

        if provider == "volcengine":
            return await self._call_volcengine(
                messages, selected_model, temperature, max_tokens, tools, tool_choice
            )
        elif provider == "gemini":
            return await self._call_gemini(
                messages, selected_model, temperature, max_tokens, tools, tool_choice
            )
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    @staticmethod
    def _extract_tool_calls(message) -> Optional[List[ToolCall]]:
        """
        从 OpenAI SDK 返回的 message 对象中提取 tool_calls。
        若无工具调用则返回 None。
        """
        if not getattr(message, "tool_calls", None):
            return None
        return [
            ToolCall(
                id=tc.id,
                type=tc.type,
                function=ToolCallFunction(
                    name=tc.function.name,
                    arguments=tc.function.arguments,
                ),
            )
            for tc in message.tool_calls
        ]

    async def _call_gemini(
        self,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> ChatResponse:
        """调用通过 Clash 代理访问的 Gemini API（支持 Tool Calling）"""
        formatted_messages = self._format_messages(messages)
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = tool_choice
        try:
            response = await self.gemini_client.chat.completions.create(**create_kwargs)
            msg = response.choices[0].message
            return ChatResponse(
                id=response.id,
                model=response.model,
                content=msg.content,
                usage=response.usage.model_dump() if response.usage else {},
                provider="gemini",
                tool_calls=self._extract_tool_calls(msg),
                finish_reason=response.choices[0].finish_reason,
            )
        except Exception as e:
            logger.error(f"Error calling Gemini API through proxy: {str(e)}")
            raise

    async def _call_volcengine(
        self,
        messages: List[Message],
        model: str,
        temperature: float,
        max_tokens: int,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
    ) -> ChatResponse:
        """调用火山引擎 API（支持 Tool Calling）"""
        formatted_messages = self._format_messages(messages)
        create_kwargs: Dict[str, Any] = {
            "model": model,
            "messages": formatted_messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if tools:
            create_kwargs["tools"] = tools
            create_kwargs["tool_choice"] = tool_choice
        try:
            response = await self.volcengine_client.chat.completions.create(**create_kwargs)
            msg = response.choices[0].message
            return ChatResponse(
                id=response.id,
                model=response.model,
                content=msg.content,
                usage=response.usage.model_dump() if response.usage else {},
                provider="volcengine",
                tool_calls=self._extract_tool_calls(msg),
                finish_reason=response.choices[0].finish_reason,
            )
        except Exception as e:
            logger.error(f"Error calling Volcengine API: {str(e)}")
            raise

# 全局单例
model_router = ModelRouter()
