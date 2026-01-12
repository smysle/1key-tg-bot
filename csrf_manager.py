"""
CSRF Token 管理模块
使用 curl_cffi 绕过 Cloudflare 保护
优化: 预刷新、缓存、非阻塞
"""
import re
import asyncio
import logging
from datetime import datetime, timedelta
from typing import Optional, Tuple

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
        self._refresh_task: Optional[asyncio.Task] = None
        self._is_refreshing = False
    
    async def _get_session(self) -> AsyncSession:
        if self._session is None:
            self._session = AsyncSession(impersonate="chrome131")
        return self._session
    
    async def _fetch_csrf_token(self) -> Tuple[str, dict]:
        """从页面获取 CSRF Token，返回 (token, cookies)"""
        url = settings.onekey_base_url
        
        session = await self._get_session()
        response = await session.get(url)
        
        if response.status_code == 403:
            raise ValueError("Access denied (403) - Cloudflare blocking")
        
        response.raise_for_status()
        html = response.text
        cookies = dict(response.cookies)
        
        # 尝试多种正则模式匹配 CSRF Token
        patterns = [
            r'window\.CSRF_TOKEN\s*=\s*["\']([^"\']+)["\']',
            r'CSRF_TOKEN["\']?\s*[:=]\s*["\']([^"\']+)["\']',
            r'csrf[_-]?token["\']?\s*[:=]\s*["\']([^"\']+)["\']',
            r'<meta\s+name=["\']csrf-token["\']\s+content=["\']([^"\']+)["\']',
            r'csrfToken["\']?\s*[:=]\s*["\']([^"\']+)["\']',
        ]
        
        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                token = match.group(1)
                logger.info(f"curl_cffi: Got cookies={list(cookies.keys())}")
                logger.info(f"Found CSRF token: {token[:20]}...")
                return token, cookies
        
        # 从 script 标签中查找
        script_pattern = r'<script[^>]*>(.*?)</script>'
        scripts = re.findall(script_pattern, html, re.DOTALL | re.IGNORECASE)
        
        for script in scripts:
            for pattern in patterns[:2]:
                match = re.search(pattern, script, re.IGNORECASE)
                if match:
                    token = match.group(1)
                    logger.info(f"Found CSRF token in script: {token[:20]}...")
                    return token, cookies
        
        logger.warning(f"CSRF token not found. Page length: {len(html)}")
        logger.debug(f"Page preview: {html[:1000]}")
        raise ValueError("CSRF token not found in page")
    
    def _should_refresh(self) -> bool:
        """检查是否需要刷新 token"""
        if self._token is None or self._last_refresh is None:
            return True
        
        # 预刷新：在过期前提前刷新
        elapsed = (datetime.now() - self._last_refresh).total_seconds()
        threshold = settings.csrf_refresh_interval - settings.csrf_preemptive_refresh
        return elapsed >= threshold
    
    async def _background_refresh(self):
        """后台刷新 token（非阻塞）"""
        if self._is_refreshing:
            return
        
        try:
            self._is_refreshing = True
            token, cookies = await self._fetch_csrf_token()
            
            async with self._lock:
                self._token = token
                self._cookies = cookies
                self._last_refresh = datetime.now()
                
        except Exception as e:
            logger.error(f"Background CSRF refresh failed: {e}")
        finally:
            self._is_refreshing = False
    
    async def get_token(self, force_refresh: bool = False) -> str:
        """获取 CSRF Token，自动处理缓存和刷新"""
        # 快速路径：token 有效且不需要刷新
        if not force_refresh and self._token and not self._should_refresh():
            return self._token
        
        async with self._lock:
            # 双重检查
            if not force_refresh and self._token and not self._should_refresh():
                return self._token
            
            # 需要刷新
            logger.info("Refreshing CSRF token...")
            token, cookies = await self._fetch_csrf_token()
            
            self._token = token
            self._cookies = cookies
            self._last_refresh = datetime.now()
            
            return self._token
    
    def get_cookies(self) -> dict:
        """获取已保存的 cookies"""
        return self._cookies.copy()
    
    def schedule_preemptive_refresh(self):
        """调度预刷新任务"""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._background_refresh())
    
    async def invalidate(self):
        """使当前 token 失效"""
        async with self._lock:
            self._token = None
            self._last_refresh = None
            self._cookies = {}
            logger.info("CSRF token invalidated")
    
    async def close(self):
        """关闭资源"""
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        
        if self._session:
            await self._session.close()
            self._session = None


# 全局单例
csrf_manager = CSRFTokenManager()
