from pydantic_settings import BaseSettings, SettingsConfigDict
from typing import List, Optional, Dict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding='utf-8',
        extra="ignore"
    )

    APP_NAME: str = "flowgpt-2api"
    APP_VERSION: str = "1.0.0"
    DESCRIPTION: str = "一个将 flowgpt.com 转换为兼容 OpenAI 格式 API 的高性能代理。"

    API_MASTER_KEY: Optional[str] = None
    
    FLOWGPT_BEARER_TOKEN: Optional[str] = None
    FLOWGPT_DEVICE_ID: Optional[str] = "aORT9gbujH92iIVYYVcTE"

    API_REQUEST_TIMEOUT: int = 180
    NGINX_PORT: int = 8088

    DEFAULT_MODEL_ID: str = "uzNcMUGo4sOwl4GFvRpm8"
    MODEL_MAP: Dict[str, str] = {
        "default": "uzNcMUGo4sOwl4GFvRpm8",
        "gpt-4-free": "uzNcMUGo4sOwl4GFvRpm8",
        "chatgpt-5-pro": "vlaOLDN9qTxzIlvzfL_Ty",
        "aisuperior": "lKm5yPE9x-Naf0_0_KI5M",
    }

settings = Settings()
