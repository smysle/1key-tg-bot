from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional, List, Union


class Settings(BaseSettings):
    # Telegram
    tg_bot_token: str = Field(..., description="Telegram Bot Token")
    
    # 1Key API
    onekey_api_key: str = Field(..., description="1Key API Key for bypassing captcha")
    onekey_base_url: str = Field(default="https://batch.1key.me", description="1Key API Base URL")
    
    # Redis (optional, for persistent stats)
    redis_url: Optional[str] = Field(default=None, description="Redis URL for persistence")
    
    # Admin - 默认管理员ID
    admin_user_ids: List[int] = Field(
        default=[6997010290],
        description="Admin user IDs"
    )
    
    # CSRF
    csrf_refresh_interval: int = Field(default=300, description="CSRF token refresh interval in seconds")
    
    # Request timeout
    request_timeout: int = Field(default=120, description="HTTP request timeout in seconds")
    
    # Batch settings
    max_batch_size: int = Field(default=5, description="Max verification IDs per batch")
    
    # Rate limiting (optional)
    user_daily_limit: int = Field(default=0, description="Max submissions per user per day (0=unlimited)")
    
    @field_validator('admin_user_ids', mode='before')
    @classmethod
    def parse_admin_ids(cls, v):
        """解析管理员ID，支持逗号分隔的字符串或单个数字"""
        if isinstance(v, list):
            return v
        if isinstance(v, int):
            return [v]
        if isinstance(v, str):
            if not v.strip():
                return [6997010290]  # 默认管理员
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return [6997010290]
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
