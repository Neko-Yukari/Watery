from pydantic_settings import BaseSettings, SettingsConfigDict
import os

class Settings(BaseSettings):
    # API Keys
    volcengine_api_key: str
    volcengine_base_url: str = "https://ark.cn-beijing.volces.com/api/coding/v3"
    gemini_api_key: str = ""
    
    # App Config
    environment: str = "development"
    log_level: str = "INFO"
    data_dir: str = "/app/data"
    
    # DB Config
    sqlite_db_name: str = "watery.db"
    vector_db_dir: str = "vector_db"

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
