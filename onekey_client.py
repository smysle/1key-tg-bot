"""
1Key API 客户端
处理批量验证、状态检查、取消等操作
"""
import re
import json
import asyncio
import logging
from typing import List, AsyncGenerator, Optional, Callable

import httpx

from config import settings
from csrf_manager import csrf_manager
from models import (
    VerificationResult,
    VerificationStep,
    CheckStatusResponse,
    CancelResponse,
)

logger = logging.getLogger(__name__)


class OneKeyAPIError(Exception):
    """1Key API 错误"""
    def __init__(self, message: str, status_code: Optional[int] = None):
        self.message = message
        self.status_code = status_code
        super().__init__(message)


class OneKeyClient:
    """1Key API 客户端"""
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "en-US,en;q=0.5",
                    "Origin": settings.onekey_base_url,
                    "Referer": f"{settings.onekey_base_url}/",
                }
            )
        return self._client
    
    async def _get_headers_with_csrf(self) -> dict:
        """获取包含 CSRF Token 的请求头"""
        csrf_token = await csrf_manager.get_token()
        return {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
        }
    
    @staticmethod
    def extract_verification_id(url_or_id: str) -> str:
        """
        从 URL 或直接的 ID 中提取 verification ID
        支持格式:
        - 直接ID: 6931007a35dfed1a6931adac
        - 完整URL: https://one.google.com/verify?...&id=xxx
        """
        # 如果已经是纯 ID（24位十六进制）
        if re.match(r'^[a-f0-9]{24}$', url_or_id, re.IGNORECASE):
            return url_or_id.lower()
        
        # 尝试从 URL 中提取
        patterns = [
            r'[?&]id=([a-f0-9]{24})',
            r'/([a-f0-9]{24})(?:[?/]|$)',
            r'([a-f0-9]{24})',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, url_or_id, re.IGNORECASE)
            if match:
                return match.group(1).lower()
        
        raise ValueError(f"无法从输入中提取 verification ID: {url_or_id[:50]}...")
    
    async def batch_verify(
        self,
        verification_ids: List[str],
        on_result: Optional[Callable[[VerificationResult], None]] = None,
        use_lucky: bool = False,
        program_id: str = "",
    ) -> AsyncGenerator[VerificationResult, None]:
        """
        批量验证（SSE 流式响应）
        
        Args:
            verification_ids: 验证ID列表（最多5个）
            on_result: 收到结果时的回调
            use_lucky: 是否使用 Lucky 模式
            program_id: 程序ID
            
        Yields:
            VerificationResult: 每个验证的结果
        """
        if len(verification_ids) > settings.max_batch_size:
            raise ValueError(f"每批最多 {settings.max_batch_size} 个验证ID")
        
        url = f"{settings.onekey_base_url}/api/batch"
        headers = await self._get_headers_with_csrf()
        
        payload = {
            "verificationIds": verification_ids,
            "hCaptchaToken": settings.onekey_api_key,
            "useLucky": use_lucky,
            "programId": program_id,
        }
        
        logger.info(f"Starting batch verification for {len(verification_ids)} IDs")
        
        try:
            async with self._http_client.stream(
                "POST",
                url,
                json=payload,
                headers=headers,
            ) as response:
                if response.status_code == 403:
                    # CSRF token 可能过期，刷新后重试
                    await csrf_manager.invalidate()
                    raise OneKeyAPIError("CSRF token expired, please retry", 403)
                
                response.raise_for_status()
                
                async for line in response.aiter_lines():
                    if not line:
                        continue
                    
                    if line.startswith("data:"):
                        data_str = line[5:].strip()
                        if not data_str:
                            continue
                        
                        try:
                            data = json.loads(data_str)
                            result = VerificationResult(
                                verificationId=data.get("verificationId", ""),
                                currentStep=data.get("currentStep", "pending"),
                                message=data.get("message", ""),
                                checkToken=data.get("checkToken"),
                            )
                            
                            if on_result:
                                on_result(result)
                            
                            yield result
                            
                        except json.JSONDecodeError as e:
                            logger.warning(f"Failed to parse SSE data: {data_str[:100]}, error: {e}")
                            continue
                            
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error in batch verify: {e}")
            raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
        except httpx.RequestError as e:
            logger.error(f"Request error in batch verify: {e}")
            raise OneKeyAPIError(f"Request error: {str(e)}")
    
    async def check_status(self, check_token: str) -> CheckStatusResponse:
        """
        检查验证状态
        
        Args:
            check_token: 从 batch_verify 结果中获取的 check token
            
        Returns:
            CheckStatusResponse: 状态检查结果
        """
        url = f"{settings.onekey_base_url}/api/check-status"
        
        payload = {"checkToken": check_token}
        
        try:
            response = await self._http_client.post(
                url,
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            
            data = response.json()
            return CheckStatusResponse(
                verificationId=data.get("verificationId", ""),
                currentStep=data.get("currentStep", "pending"),
                message=data.get("message", ""),
                checkToken=data.get("checkToken"),
            )
            
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error in check status: {e}")
            raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
        except httpx.RequestError as e:
            logger.error(f"Request error in check status: {e}")
            raise OneKeyAPIError(f"Request error: {str(e)}")
    
    async def poll_until_complete(
        self,
        check_token: str,
        interval: float = 3.0,
        max_attempts: int = 60,
        on_update: Optional[Callable[[CheckStatusResponse], None]] = None,
    ) -> CheckStatusResponse:
        """
        轮询直到验证完成
        
        Args:
            check_token: 初始 check token
            interval: 轮询间隔（秒）
            max_attempts: 最大尝试次数
            on_update: 状态更新回调
            
        Returns:
            CheckStatusResponse: 最终状态
        """
        current_token = check_token
        
        for attempt in range(max_attempts):
            result = await self.check_status(current_token)
            
            if on_update:
                on_update(result)
            
            if result.current_step != VerificationStep.PENDING:
                return result
            
            # 更新 token 用于下次轮询
            if result.check_token:
                current_token = result.check_token
            
            await asyncio.sleep(interval)
        
        raise OneKeyAPIError(f"Polling timeout after {max_attempts} attempts")
    
    async def cancel_verification(self, verification_id: str) -> CancelResponse:
        """
        取消验证
        
        Args:
            verification_id: 验证ID
            
        Returns:
            CancelResponse: 取消结果
        """
        url = f"{settings.onekey_base_url}/api/cancel"
        headers = await self._get_headers_with_csrf()
        
        payload = {"verificationId": verification_id}
        
        try:
            response = await self._http_client.post(
                url,
                json=payload,
                headers=headers,
            )
            
            if response.status_code == 403:
                await csrf_manager.invalidate()
                raise OneKeyAPIError("CSRF token expired, please retry", 403)
            
            response.raise_for_status()
            
            data = response.json()
            return CancelResponse(
                verificationId=data.get("verificationId", ""),
                currentStep=data.get("currentStep", "error"),
                message=data.get("message", ""),
                alreadyCancelled=data.get("alreadyCancelled", False),
            )
            
        except httpx.HTTPStatusError as e:
            logger.error(f"HTTP error in cancel: {e}")
            raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
        except httpx.RequestError as e:
            logger.error(f"Request error in cancel: {e}")
            raise OneKeyAPIError(f"Request error: {str(e)}")
    
    async def close(self):
        """关闭客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        await csrf_manager.close()


# 全局单例
onekey_client = OneKeyClient()
