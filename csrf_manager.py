"""
CSRF Token 管理模块
使用 curl_cffi 绕过 Cloudflare 保护
"""
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional

from curl_cffi.requests import AsyncSession

from config import settings

logger = logging.getLogger(__name__)


class CSRFTokenManager:
    """CSRF Token 管理器，自动刷新和缓存"""
    
    def __init__(self):
        self._token: Optional[str] = None
        self._cookies: dict = {}
        self._last_refresh: Optional[datetime] = None
        self._lock = asyncio.Lock()
        self._session: Optional[AsyncSession] = None
    
    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome131")
        return self._session
    
    async def _fetch_csrf_token(self) -> str:
        """从页面获取 CSRF Token"""
        url = settings.onekey_base_url
        
        try:
            session = await self._get_session()
            response = await session.get(url)
            
            if response.status_code == 403:
                raise ValueError(f"Access denied (403) - Cloudflare may be blocking")
            
            response.raise_for_status()
            html = response.text
            
            # 保存 cookies 供后续请求使用
            self._cookies = dict(response.cookies)
            logger.info(f"Got cookies: {list(self._cookies.keys())}")
            
            # 尝试多种正则模式匹配 CSRF Token
            patterns = [
                r'window\.CSRF_TOKEN\s*=\s*["\']([^"\']+)["\']',
                r'CSRF_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'csrf[_-]?token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
                r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
                r'_csrf["\']?\s*[:=]\s*["\']([^"\']+)["\']',
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
                for pattern in patterns[:2]:
                    match = re.search(pattern, script, re.IGNORECASE)
                    if match:
                        token = match.group(1)
                        logger.info(f"Found CSRF token in script: {token[:20]}...")
                        return token
            
            # 调试：输出页面更多内容
            logger.warning(f"CSRF token not found. Page length: {len(html)}")
            logger.warning(f"Page preview: {html[:2000]}")
            raise ValueError("CSRF token not found in page")
            
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
    
    def get_cookies(self) -> dict:
        """获取已保存的 cookies"""
        return self._cookies.copy()
    
    async def invalidate(self):
        """使当前 token 失效，下次获取时强制刷新"""
        async with self._lock:
            self._token = None
            self._last_refresh = None
            self._cookies = {}
            logger.info("CSRF token invalidated")
    
    async def close(self):
        """关闭 HTTP 客户端"""
        if self._session:
            await self._session.close()
            self._session = None


# 全局单例
csrf_manager = CSRFTokenManager()
