from pydantic_settings import BaseSettings
from pydantic import Field, field_validator
from typing import Optional, List


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
    csrf_preemptive_refresh: int = Field(default=60, description="Preemptive refresh before expiry (seconds)")
    
    # Request timeout
    request_timeout: int = Field(default=120, description="HTTP request timeout in seconds")
    
    # Batch settings
    max_batch_size: int = Field(default=5, description="Max verification IDs per batch")
    
    # Concurrency settings (NEW)
    max_concurrent_requests: int = Field(default=10, description="Max concurrent API requests")
    max_concurrent_polls: int = Field(default=20, description="Max concurrent status polls")
    poll_interval: float = Field(default=2.0, description="Status poll interval in seconds")
    poll_max_attempts: int = Field(default=90, description="Max poll attempts before timeout")
    
    # Retry settings (NEW)
    max_retries: int = Field(default=3, description="Max retries for failed requests")
    retry_delay: float = Field(default=1.0, description="Initial retry delay in seconds")
    retry_backoff: float = Field(default=2.0, description="Retry delay backoff multiplier")
    
    # Rate limiting (optional)
    user_daily_limit: int = Field(default=0, description="Max submissions per user per day (0=unlimited)")
    
    # Connection pool (NEW)
    connection_pool_size: int = Field(default=20, description="HTTP connection pool size")
    
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
                return [6997010290]
            return [int(x.strip()) for x in v.split(",") if x.strip()]
        return [6997010290]
    
    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()
