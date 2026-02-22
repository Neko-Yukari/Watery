from pydantic import BaseModel, Field
from typing import List, Optional, Dict, Any

class Message(BaseModel):
    role: str = Field(..., description="角色，如 'system', 'user', 'assistant'")
    content: str = Field(..., description="消息内容")

class ChatRequest(BaseModel):
    messages: List[Message]
    model: str = Field("ark-code-latest", description="指定使用的模型名称，默认使用火山引擎自动化模型 ark-code-latest")
    temperature: Optional[float] = Field(0.7, description="生成温度")
    max_tokens: Optional[int] = Field(2048, description="最大生成 token 数")

class ChatResponse(BaseModel):
    id: str
    model: str
    content: str
    usage: Dict[str, Any]
    provider: str = Field(..., description="提供商，如 'volcengine', 'gemini'")

class SkillManifest(BaseModel):
    id: str
    name: str = Field(..., description="技能名称")
    description: str = Field(..., description="技能描述，用于语义检索")
    language: str = Field("python", description="执行环境，如 'python', 'shell', 'nodejs'")
    content: str = Field(..., description="技能代码或执行逻辑")
    
class ErrorLog(BaseModel):
    id: Optional[str] = None
    context: str = Field(..., description="触发错误的上下文描述")
    correction: str = Field(..., description="纠错后的建议或代码")
    skill_id: Optional[str] = None

class IntentionRequest(BaseModel):
    intention: str = Field(..., description="用户意图描述")
