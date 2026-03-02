from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import Optional
import os

class Settings(BaseSettings):
    # API Keys
    volcengine_api_key: str
    volcengine_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    gemini_api_key: str = ""

    # Phase 5: ms-agent 搜索引擎 API Key（可选，deep_research 用）
    exa_api_key: Optional[str] = None          # https://exa.ai — 高质量网络搜索
    serpapi_api_key: Optional[str] = None       # https://serpapi.com — Google 搜索备选
    modelscope_api_key: Optional[str] = None    # ModelScope 平台（可选）

    # App Config
    environment: str = "development"
    log_level: str = "INFO"
    data_dir: str = "/app/data"

    # DB Config
    sqlite_db_name: str = "watery.db"
    vector_db_dir: str = "vector_db"

    # Proxy / Clash Config
    clash_api_url: str = "http://clash:9090"
    proxy_url: str = "http://clash:7890"
    subscription_url: Optional[str] = None        # 未配置时 ProxyManager 跳过订阅更新
    proxy_region_filter: str = "美国|US"          # 节点过滤关键词（正则）
    clash_config_path: str = "/app/data/clash/config.yaml"

    # Feishu (Lark) Webhook Config
    feishu_webhook_url: Optional[str] = None      # 飞书自定义机器人 Webhook URL
    feishu_webhook_secret: Optional[str] = None   # 飞书签名校验密钥（可选）

    @property
    def sqlite_db_path(self) -> str:
        db_file = os.path.join(self.data_dir, self.sqlite_db_name)
        # 兼容 Windows 盘符和 Linux 路径，统一转换
        abs_path = os.path.abspath(db_file)
        return f"sqlite:///{abs_path}"

    @property
    def vector_db_path(self) -> str:
        return os.path.join(self.data_dir, self.vector_db_dir)

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

# 实例化配置对象
settings = Settings()

# 确保数据目录存在
os.makedirs(settings.data_dir, exist_ok=True)
