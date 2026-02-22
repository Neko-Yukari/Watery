import logging
import httpx
import os
from typing import List, Dict, Any, Optional
from openai import AsyncOpenAI
from app.core.config import settings
from app.models.schemas import Message, ChatResponse

logger = logging.getLogger(__name__)

class ModelRouter:
    def __init__(self):
        # 初始化火山引擎客户端 (直连)
        self.volcengine_client = AsyncOpenAI(
            api_key=settings.volcengine_api_key,
            base_url=settings.volcengine_base_url
        )
        
        # 为 Gemini 初始化代理客户端
        proxy_url = os.getenv("GEMINI_PROXY_URL")
        # 创建一个受代理支持的 HTTP 客户端
        self.gemini_proxy_transport = httpx.AsyncHTTPTransport(proxy=proxy_url) if proxy_url else None
        
        # 即使 GEMINI_API_KEY 已填，也支持后期通过 .env 加载
        self.gemini_client = AsyncOpenAI(
            api_key=settings.gemini_api_key,
            base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
            http_client=httpx.AsyncClient(transport=self.gemini_proxy_transport) if self.gemini_proxy_transport else None
        )

        # 维护可用模型列表
        self.available_models = {
            "volcengine": [
                "ark-code-latest",
                "doubao-seed-code"
            ],
            "gemini": [
                "gemini-1.5-pro",
                "gemini-1.5-flash",
                "gemini-pro"
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

    async def generate(self, messages: List[Message], model: Optional[str] = None, temperature: float = 0.7, max_tokens: int = 2048) -> ChatResponse:
        provider, selected_model = self._select_model(model)
        
        logger.info(f"Routing request to provider: {provider}, model: {selected_model}")

        if provider == "volcengine":
            return await self._call_volcengine(messages, selected_model, temperature, max_tokens)
        elif provider == "gemini":
            return await self._call_gemini(messages, selected_model, temperature, max_tokens)
        else:
            raise ValueError(f"Unsupported provider: {provider}")

    async def _call_gemini(self, messages: List[Message], model: str, temperature: float, max_tokens: int) -> ChatResponse:
        """调用通过 Clash 代理访问的 Gemini API"""
        formatted_messages = [{"role": msg.role, "content": msg.content} for msg in messages]
        try:
            response = await self.gemini_client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            return ChatResponse(
                id=response.id,
                model=response.model,
                content=response.choices[0].message.content,
                usage=response.usage.model_dump() if response.usage else {},
                provider="gemini"
            )
        except Exception as e:
            logger.error(f"Error calling Gemini API through proxy: {str(e)}")
            raise

    async def _call_volcengine(self, messages: List[Message], model: str, temperature: float, max_tokens: int) -> ChatResponse:
        formatted_messages = [{"role": msg.role, "content": msg.content} for msg in messages]
        
        try:
            response = await self.volcengine_client.chat.completions.create(
                model=model,
                messages=formatted_messages,
                temperature=temperature,
                max_tokens=max_tokens
            )
            
            return ChatResponse(
                id=response.id,
                model=response.model,
                content=response.choices[0].message.content,
                usage=response.usage.model_dump() if response.usage else {},
                provider="volcengine"
            )
        except Exception as e:
            logger.error(f"Error calling Volcengine API: {str(e)}")
            raise

# 全局单例
model_router = ModelRouter()
