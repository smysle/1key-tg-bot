"""
数据模型定义
"""
from enum import Enum
from typing import Optional, List, Any
from pydantic import BaseModel, Field, field_validator
from datetime import datetime


class VerificationStep(str, Enum):
    """验证步骤/状态"""
    PENDING = "pending"
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # 处理未知状态


class BatchRequest(BaseModel):
    """批量验证请求"""
    verification_ids: List[str] = Field(..., alias="verificationIds")
    hcaptcha_token: str = Field(..., alias="hCaptchaToken")
    use_lucky: bool = Field(default=False, alias="useLucky")
    program_id: str = Field(default="", alias="programId")
    
    class Config:
        populate_by_name = True


class VerificationResult(BaseModel):
    """单个验证结果"""
    verification_id: str = Field(..., alias="verificationId")
    current_step: VerificationStep = Field(..., alias="currentStep")
    message: str = ""
    check_token: Optional[str] = Field(default=None, alias="checkToken")
    
    @field_validator('current_step', mode='before')
    @classmethod
    def validate_step(cls, v: Any) -> VerificationStep:
        if isinstance(v, VerificationStep):
            return v
        if not v or v == "":
            return VerificationStep.UNKNOWN
        try:
            return VerificationStep(v)
        except ValueError:
            return VerificationStep.UNKNOWN
    
    class Config:
        populate_by_name = True


class CheckStatusRequest(BaseModel):
    """状态检查请求"""
    check_token: str = Field(..., alias="checkToken")
    
    class Config:
        populate_by_name = True


class CheckStatusResponse(BaseModel):
    """状态检查响应"""
    verification_id: str = Field(..., alias="verificationId")
    current_step: VerificationStep = Field(..., alias="currentStep")
    message: str = ""
    check_token: Optional[str] = Field(default=None, alias="checkToken")
    
    @field_validator('current_step', mode='before')
    @classmethod
    def validate_step(cls, v: Any) -> VerificationStep:
        if isinstance(v, VerificationStep):
            return v
        if not v or v == "":
            return VerificationStep.UNKNOWN
        try:
            return VerificationStep(v)
        except ValueError:
            return VerificationStep.UNKNOWN
    
    class Config:
        populate_by_name = True


class CancelRequest(BaseModel):
    """取消验证请求"""
    verification_id: str = Field(..., alias="verificationId")
    
    class Config:
        populate_by_name = True


class CancelResponse(BaseModel):
    """取消验证响应"""
    verification_id: str = Field(..., alias="verificationId")
    current_step: VerificationStep = Field(..., alias="currentStep")
    message: str = ""
    already_cancelled: bool = Field(default=False, alias="alreadyCancelled")
    
    @field_validator('current_step', mode='before')
    @classmethod
    def validate_step(cls, v: Any) -> VerificationStep:
        if isinstance(v, VerificationStep):
            return v
        if not v or v == "":
            return VerificationStep.ERROR
        try:
            return VerificationStep(v)
        except ValueError:
            return VerificationStep.ERROR
    
    class Config:
        populate_by_name = True


class VerificationTask(BaseModel):
    """验证任务记录（用于持久化）"""
    verification_id: str
    user_id: int  # Telegram user ID
    chat_id: int  # Telegram chat ID
    status: VerificationStep = VerificationStep.PENDING
    check_token: Optional[str] = None
    message: str = ""
    created_at: datetime = Field(default_factory=datetime.now)
    updated_at: datetime = Field(default_factory=datetime.now)
    
    def update_from_result(self, result: VerificationResult):
        """从验证结果更新任务"""
        self.status = result.current_step
        self.message = result.message
        self.check_token = result.check_token
        self.updated_at = datetime.now()
