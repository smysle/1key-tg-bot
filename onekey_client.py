"""
1Key API 客户端
优化: 连接池、重试、并发控制、去重
"""
import re
import json
import asyncio
import logging
from typing import List, AsyncGenerator, Optional, Callable, Set, Dict
from contextlib import asynccontextmanager

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
    def __init__(self, message: str, status_code: Optional[int] = None, retryable: bool = False):
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        super().__init__(message)


class OneKeyClient:
    """1Key API 客户端 - 高性能版本"""
    
    def __init__(self):
        self._client: Optional[httpx.AsyncClient] = None
        self._request_semaphore: Optional[asyncio.Semaphore] = None
        self._poll_semaphore: Optional[asyncio.Semaphore] = None
        self._pending_ids: Set[str] = set()
        self._pending_lock = asyncio.Lock()
    
    @property
    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            limits = httpx.Limits(
                max_keepalive_connections=settings.connection_pool_size,
                max_connections=settings.connection_pool_size + 10,
                keepalive_expiry=30.0,
            )
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
                follow_redirects=True,
                limits=limits,
                http2=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                    "Accept": "*/*",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Origin": settings.onekey_base_url,
                    "Referer": f"{settings.onekey_base_url}/",
                    "Sec-Ch-Ua": '"Google Chrome";v="120", "Chromium";v="120", "Not_A Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "empty",
                    "Sec-Fetch-Mode": "cors",
                    "Sec-Fetch-Site": "same-origin",
                }
            )
        return self._client
    
    @property
    def request_semaphore(self) -> asyncio.Semaphore:
        if self._request_semaphore is None:
            self._request_semaphore = asyncio.Semaphore(settings.max_concurrent_requests)
        return self._request_semaphore
    
    @property
    def poll_semaphore(self) -> asyncio.Semaphore:
        if self._poll_semaphore is None:
            self._poll_semaphore = asyncio.Semaphore(settings.max_concurrent_polls)
        return self._poll_semaphore
    
    async def _get_headers_with_csrf(self) -> dict:
        csrf_token = await csrf_manager.get_token()
        return {
            "Content-Type": "application/json",
            "X-CSRF-Token": csrf_token,
        }
    
    async def _retry_request(self, request_func: Callable, *args, max_retries: Optional[int] = None, **kwargs):
        max_retries = max_retries or settings.max_retries
        delay = settings.retry_delay
        
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                return await request_func(*args, **kwargs)
            except (httpx.RequestError, httpx.HTTPStatusError) as e:
                last_error = e
                status_code = getattr(e, 'response', None)
                status_code = status_code.status_code if status_code else None
                
                if status_code in (400, 401, 403, 404):
                    raise
                
                if attempt < max_retries:
                    logger.warning(f"Request failed (attempt {attempt + 1}/{max_retries + 1}): {e}")
                    await asyncio.sleep(delay)
                    delay *= settings.retry_backoff
        
        raise last_error
    
    async def check_duplicate(self, verification_id: str) -> bool:
        async with self._pending_lock:
            if verification_id in self._pending_ids:
                return True
            self._pending_ids.add(verification_id)
            return False
    
    async def remove_pending(self, verification_id: str):
        async with self._pending_lock:
            self._pending_ids.discard(verification_id)
    
    @staticmethod
    def extract_verification_id(url_or_id: str) -> str:
        if re.match(r'^[a-f0-9]{24}$', url_or_id, re.IGNORECASE):
            return url_or_id.lower()
        
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
        if len(verification_ids) > settings.max_batch_size:
            raise ValueError(f"每批最多 {settings.max_batch_size} 个验证ID")
        
        url = f"{settings.onekey_base_url}/api/batch"
        
        async with self.request_semaphore:
            headers = await self._get_headers_with_csrf()
            payload = {
                "verificationIds": verification_ids,
                "hCaptchaToken": settings.onekey_api_key,
                "useLucky": use_lucky,
                "programId": program_id,
            }
            
            logger.info(f"Starting batch verification for {len(verification_ids)} IDs")
            
            try:
                async with self._http_client.stream("POST", url, json=payload, headers=headers) as response:
                    if response.status_code == 403:
                        await csrf_manager.invalidate()
                        raise OneKeyAPIError("CSRF token expired", 403, retryable=True)
                    
                    response.raise_for_status()
                    
                    async for line in response.aiter_lines():
                        if not line or not line.startswith("data:"): continue
                        
                        data_str = line[5:].strip()
                        if not data_str: continue
                        
                        try:
                            data = json.loads(data_str)
                            result = VerificationResult(
                                verificationId=data.get("verificationId", ""),
                                currentStep=data.get("currentStep", "pending"),
                                message=data.get("message", ""),
                                checkToken=data.get("checkToken"),
                            )
                            if on_result: on_result(result)
                            yield result
                        except json.JSONDecodeError:
                            continue
                            
            except httpx.HTTPStatusError as e:
                logger.error(f"HTTP error: {e}")
                raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
            except httpx.RequestError as e:
                logger.error(f"Request error: {e}")
                raise OneKeyAPIError(f"Request error: {str(e)}", retryable=True)
    
    async def check_status(self, check_token: str) -> CheckStatusResponse:
        url = f"{settings.onekey_base_url}/api/check-status"
        async with self.poll_semaphore:
            try:
                response = await self._retry_request(
                    self._http_client.post,
                    url,
                    json={"checkToken": check_token},
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
                raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
            except httpx.RequestError as e:
                raise OneKeyAPIError(f"Request error: {str(e)}", retryable=True)

    async def cancel_verification(self, verification_id: str) -> CancelResponse:
        url = f"{settings.onekey_base_url}/api/cancel"
        async with self.request_semaphore:
            headers = await self._get_headers_with_csrf()
            try:
                response = await self._retry_request(
                    self._http_client.post,
                    url,
                    json={"verificationId": verification_id},
                    headers=headers,
                )
                if response.status_code == 403:
                    await csrf_manager.invalidate()
                    raise OneKeyAPIError("CSRF token expired", 403, retryable=True)
                
                response.raise_for_status()
                data = response.json()
                return CancelResponse(
                    verificationId=data.get("verificationId", ""),
                    currentStep=data.get("currentStep", "error"),
                    message=data.get("message", ""),
                    alreadyCancelled=data.get("alreadyCancelled", False),
                )
            except httpx.HTTPStatusError as e:
                raise OneKeyAPIError(f"HTTP error: {e.response.status_code}", e.response.status_code)
            except httpx.RequestError as e:
                raise OneKeyAPIError(f"Request error: {str(e)}", retryable=True)
    
    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
        await csrf_manager.close()

onekey_client = OneKeyClient()
