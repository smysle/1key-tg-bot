"""
用户统计存储模块
支持 Redis（推荐）或内存存储
"""
import asyncio
import json
import logging
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class StatsStorage(ABC):
    """统计存储抽象基类"""
    
    @abstractmethod
    async def record_submission(self, user_id: int, count: int = 1) -> None:
        """记录用户提交"""
        pass
    
    @abstractmethod
    async def get_user_stats(self, user_id: int) -> Dict:
        """获取单个用户统计"""
        pass
    
    @abstractmethod
    async def get_all_stats(self) -> Dict:
        """获取所有用户统计"""
        pass
    
    @abstractmethod
    async def get_24h_stats(self) -> Dict:
        """获取24小时统计"""
        pass


class MemoryStatsStorage(StatsStorage):
    """内存统计存储（无持久化）"""
    
    def __init__(self):
        self._total_counts: Dict[int, int] = defaultdict(int)  # user_id -> total count
        self._submissions: List[Tuple[int, datetime, int]] = []  # (user_id, timestamp, count)
        self._lock = asyncio.Lock()
    
    async def record_submission(self, user_id: int, count: int = 1) -> None:
        async with self._lock:
            self._total_counts[user_id] += count
            self._submissions.append((user_id, datetime.now(), count))
            
            # 清理超过24小时的记录
            cutoff = datetime.now() - timedelta(hours=24)
            self._submissions = [s for s in self._submissions if s[1] > cutoff]
    
    async def get_user_stats(self, user_id: int) -> Dict:
        async with self._lock:
            total = self._total_counts.get(user_id, 0)
            
            cutoff = datetime.now() - timedelta(hours=24)
            count_24h = sum(s[2] for s in self._submissions if s[0] == user_id and s[1] > cutoff)
            
            return {
                "user_id": user_id,
                "total": total,
                "last_24h": count_24h,
            }
    
    async def get_all_stats(self) -> Dict:
        async with self._lock:
            total_all = sum(self._total_counts.values())
            user_count = len(self._total_counts)
            
            # Top 10 users
            top_users = sorted(
                self._total_counts.items(),
                key=lambda x: x[1],
                reverse=True
            )[:10]
            
            return {
                "total_submissions": total_all,
                "total_users": user_count,
                "top_users": [{"user_id": u, "count": c} for u, c in top_users],
            }
    
    async def get_24h_stats(self) -> Dict:
        async with self._lock:
            cutoff = datetime.now() - timedelta(hours=24)
            
            # 24小时内的统计
            counts_24h: Dict[int, int] = defaultdict(int)
            for user_id, ts, count in self._submissions:
                if ts > cutoff:
                    counts_24h[user_id] += count
            
            total_24h = sum(counts_24h.values())
            user_count_24h = len(counts_24h)
            
            # Top 10 users in 24h
            top_users = sorted(
                counts_24h.items(),
                key=lambda x: x[1],
                reverse=True
            )[:10]
            
            return {
                "total_24h": total_24h,
                "users_24h": user_count_24h,
                "top_users_24h": [{"user_id": u, "count": c} for u, c in top_users],
            }


class RedisStatsStorage(StatsStorage):
    """Redis 统计存储（持久化）"""
    
    def __init__(self, redis_url: str):
        self.redis_url = redis_url
        self._redis = None
    
    async def _get_redis(self):
        if self._redis is None:
            import redis.asyncio as aioredis
            self._redis = await aioredis.from_url(self.redis_url, decode_responses=True)
        return self._redis
    
    async def record_submission(self, user_id: int, count: int = 1) -> None:
        r = await self._get_redis()
        
        # 总计数
        await r.hincrby("1key:stats:total", str(user_id), count)
        
        # 24小时滑动窗口 (使用 sorted set, score 为时间戳)
        now = datetime.now().timestamp()
        member = f"{user_id}:{now}:{count}"
        await r.zadd("1key:stats:24h", {member: now})
        
        # 清理24小时前的数据
        cutoff = now - 86400
        await r.zremrangebyscore("1key:stats:24h", 0, cutoff)
    
    async def get_user_stats(self, user_id: int) -> Dict:
        r = await self._get_redis()
        
        # 总计数
        total = await r.hget("1key:stats:total", str(user_id))
        total = int(total) if total else 0
        
        # 24小时计数
        now = datetime.now().timestamp()
        cutoff = now - 86400
        
        all_24h = await r.zrangebyscore("1key:stats:24h", cutoff, now)
        count_24h = sum(
            int(m.split(":")[2])
            for m in all_24h
            if m.split(":")[0] == str(user_id)
        )
        
        return {
            "user_id": user_id,
            "total": total,
            "last_24h": count_24h,
        }
    
    async def get_all_stats(self) -> Dict:
        r = await self._get_redis()
        
        # 获取所有用户总计数
        all_totals = await r.hgetall("1key:stats:total")
        
        total_all = sum(int(v) for v in all_totals.values())
        user_count = len(all_totals)
        
        # Top 10
        top_users = sorted(
            [(int(k), int(v)) for k, v in all_totals.items()],
            key=lambda x: x[1],
            reverse=True
        )[:10]
        
        return {
            "total_submissions": total_all,
            "total_users": user_count,
            "top_users": [{"user_id": u, "count": c} for u, c in top_users],
        }
    
    async def get_24h_stats(self) -> Dict:
        r = await self._get_redis()
        
        now = datetime.now().timestamp()
        cutoff = now - 86400
        
        all_24h = await r.zrangebyscore("1key:stats:24h", cutoff, now)
        
        # 按用户聚合
        counts_24h: Dict[int, int] = defaultdict(int)
        for m in all_24h:
            parts = m.split(":")
            user_id = int(parts[0])
            count = int(parts[2])
            counts_24h[user_id] += count
        
        total_24h = sum(counts_24h.values())
        user_count_24h = len(counts_24h)
        
        top_users = sorted(
            counts_24h.items(),
            key=lambda x: x[1],
            reverse=True
        )[:10]
        
        return {
            "total_24h": total_24h,
            "users_24h": user_count_24h,
            "top_users_24h": [{"user_id": u, "count": c} for u, c in top_users],
        }
    
    async def close(self):
        if self._redis:
            await self._redis.close()


def create_stats_storage(redis_url: Optional[str] = None) -> StatsStorage:
    """创建统计存储实例"""
    if redis_url:
        logger.info("Using Redis for stats storage")
        return RedisStatsStorage(redis_url)
    else:
        logger.info("Using in-memory stats storage (no persistence)")
        return MemoryStatsStorage()
