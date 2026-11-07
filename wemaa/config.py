"""WeMaa 配置；兼容已有 Windows 机器令牌与原生 API 令牌。"""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")
    WEMAA_DATA_DIR: Path = Path("data/wemaa")
    WEMAA_ACCOUNT_ID: str = "win-wechat-maa"
    WEMAA_ADMIN_USER_ID: int = 1
    WEMAA_ENABLED: bool = True
    WEMAA_DATABASE_URL: str = "sqlite:///data/wemaa/wemaa.sqlite3"
    REDIS_URL: str = "redis://127.0.0.1:6379/0"
    WECHAT_MAA_TOKEN: str = ""
    WECHAT_MAA_API_TOKEN: str = ""
    WECHAT_MAA_REPLY_ENABLED: bool = False
    WECHAT_MAA_CREATION_ENABLED: bool = False
    MINIO_ENDPOINT: str = "http://127.0.0.1:9000"
    MINIO_ACCESS_KEY: str = ""
    MINIO_SECRET_KEY: str = ""
    MINIO_REGION: str = "us-east-1"
    WEMAA_WEBSITE_ENABLED: bool = False
    WEMAA_WEBSITE_URL: str = ""
    WEMAA_WEBSITE_TOKEN: str = ""
    WEMAA_WEBSITE_ASSET_BUCKET: str = "wemaa"
    WEMAA_MODEL_PROXY_URL: str = ""
    WEMAA_NATIVE_MODELS_FILE: str = "/MaiMBot/config/native_models.json"
    SUPPORT_BRAIN_ADAPTER_TOKEN: str = ""
    SUPPORT_MODEL_PROXY_TOKEN: str = ""
    SUPPORT_BRAIN_TRACE_SECRET: str = ""
    SUPPORT_BRAIN_REQUEST_CHANNEL: str = "support:brain:requests"


settings = Settings()
