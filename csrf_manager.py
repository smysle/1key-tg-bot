"""
CSRF Token 管理模块
通过正则匹配从 1Key 页面获取 CSRF Token
"""
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

import httpx

from config import settings

logger = logging.getLogger(__name__)


class CSRFTokenManager:
    """CSRF Token 管理器，自动刷新和缓存"""
    
    def __init__(self):
        self._token: Optional[str] = None
        self._last_refresh: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._client: Optional[httpx.AsyncClient] = None
    
    @property
    def _http_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=True,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
                    "Accept-Encoding": "gzip, deflate, br",
                    "Cache-Control": "no-cache",
                    "Pragma": "no-cache",
                    "Sec-Ch-Ua": '"Google Chrome";v="131", "Chromium";v="131", "Not_A Brand";v="24"',
                    "Sec-Ch-Ua-Mobile": "?0",
                    "Sec-Ch-Ua-Platform": '"Windows"',
                    "Sec-Fetch-Dest": "document",
                    "Sec-Fetch-Mode": "navigate",
                    "Sec-Fetch-Site": "none",
                    "Sec-Fetch-User": "?1",
                    "Upgrade-Insecure-Requests": "1",
                }
            )
        return self._client
        return self._client
    
    async def _fetch_csrf_token(self) -> str:
        """从页面获取 CSRF Token"""
        url = settings.onekey_base_url
        
        try:
            response = await self._http_client.get(url)
            response.raise_for_status()
            html = response.text
            
            # 尝试多种正则模式匹配 CSRF Token
            patterns = [
                r'window\.CSRF_TOKEN\s*=\s*["\']([^"\']+)["\']',
                r'CSRF_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'csrf[_-]?token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
                r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',  # from user suggestion
                r'_csrf["\']?\s*[:=]\s*["\']([^"\']+)["\']',      # from user suggestion
            ]
            
            for pattern in patterns:
                match = re.search(pattern, html, re.IGNORECASE)
                if match:
                    token = match.group(1)
                    logger.info(f"Successfully fetched CSRF token: {token[:20]}...")
                    return token
            
            # 如果没找到，尝试从 script 标签中查找
            script_pattern = r'<script[^>]*>(.*?)</script>'
            scripts = re.findall(script_pattern, html, re.DOTALL | re.IGNORECASE)
            
            for script in scripts:
                for pattern in patterns[:2]:  # 只用前两个模式
                    match = re.search(pattern, script, re.IGNORECASE)
                    if match:
                        token = match.group(1)
                        logger.info(f"Found CSRF token in script: {token[:20]}...")
                        return token
            
            raise ValueError("CSRF token not found in page")
            
        except httpx.HTTPError as e:
            logger.error(f"HTTP error fetching CSRF token: {e}")
            raise
        except Exception as e:
            logger.error(f"Error fetching CSRF token: {e}")
            raise
    
    async def get_token(self, force_refresh: bool = False) -> str:
        """获取 CSRF Token，自动处理缓存和刷新"""
        async with self._lock:
            now = datetime.now()
            
            # 检查是否需要刷新
            needs_refresh = (
                force_refresh
                or self._token is None
                or self._last_refresh is None
                or (now - self._last_refresh) > timedelta(seconds=settings.csrf_refresh_interval)
            )
            
            if needs_refresh:
                logger.info("Refreshing CSRF token...")
                self._token = await self._fetch_csrf_token()
                self._last_refresh = now
            
            return self._token
    
    async def invalidate(self):
        """使当前 token 失效，下次获取时强制刷新"""
        async with self._lock:
            self._token = None
            self._last_refresh = None
            logger.info("CSRF token invalidated")
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._client and not self._client.is_closed:
            await self._client.aclose()


# 全局单例
csrf_manager = CSRFTokenManager()
