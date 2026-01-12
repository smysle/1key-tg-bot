"""
1Key Google 学生认证 Telegram Bot
优化版: 并发轮询、优雅关闭、更好的错误处理

命令:
/start - 开始使用
/help - 帮助信息
/verify <url或id> - 提交验证
/status <verification_id> - 查询验证状态
/cancel <verification_id> - 取消验证
/batch <url1> <url2> ... - 批量验证（最多5个）
/mystats - 查看个人统计

管理员命令:
/stats - 查看全局统计
/stats24 - 查看24小时统计
/user <user_id> - 查看指定用户统计
"""
import re
import asyncio
import logging
import signal
from typing import List, Dict, Optional
from functools import wraps
from contextlib import suppress

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

from config import settings
from onekey_client import onekey_client, OneKeyAPIError, OneKeyClient
from models import VerificationStep, VerificationResult
from stats_storage import create_stats_storage, StatsStorage

# 配置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# 关闭 httpx 的 INFO 日志
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# 全局
stats_storage: Optional[StatsStorage] = None
shutdown_event = asyncio.Event()

# 状态 emoji 映射
STATUS_EMOJI = {
    VerificationStep.PENDING: "⏳",
    VerificationStep.SUCCESS: "✅",
    VerificationStep.ERROR: "❌",
    VerificationStep.CANCELLED: "🚫",
}


def escape_markdown(text: str) -> str:
    """转义 Markdown V2 特殊字符"""
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars:
        text = text.replace(char, f'\{char}')
    return text


def admin_required(func):
    """管理员权限装饰器"""
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        user_id = update.effective_user.id
        if user_id not in settings.admin_user_ids:
            await update.message.reply_text("❌ 此命令仅限管理员使用")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /start 命令"""
    user_id = update.effective_user.id
    is_admin = user_id in settings.admin_user_ids
    
    admin_section = ""
    if is_admin:
        admin_section = """
*管理员命令:*
• /stats \- 全局统计
• /stats24 \- 24小时统计
• /user \- 查看用户统计
"""
    
    welcome_text = f"""
🎓 *1Key Google 学生认证 Bot*

欢迎使用！本 Bot 帮助您批量验证 Google 学生认证\.

*使用方法:*
1️⃣ 发送 /verify 开始验证
2️⃣ 或直接发送验证链接/ID（每行一个）
3️⃣ 使用 /batch 一次验证多个（最多5个）

*命令列表:*
• /verify \- 提交单个或多个验证
• /batch \- 批量验证
• /status \- 查询验证状态
• /cancel \- 取消验证
• /mystats \- 个人统计
• /help \- 查看帮助
{admin_section}
*Tips:*
• 支持直接粘贴 Google One 链接
• 支持直接粘贴 24位验证ID
• 每批最多 5 个验证ID
    """
    await update.message.reply_text(welcome_text, parse_mode=ParseMode.MARKDOWN_V2)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    help_text = """
📖 *帮助信息*

*验证命令:*
`/verify` \- 验证单个或多个
`/batch` \- 批量验证（最多5个）

*查询和管理:*
`/status` \- 查询状态
`/cancel` \- 取消验证
`/mystats` \- 个人统计

*支持的输入格式:*
• 完整链接或验证ID都可以
• 验证ID示例: `6931007a35dfed1a6931adac`

*状态说明:*
⏳ pending \- 处理中
✅ success \- 成功
❌ error \- 失败
🚫 cancelled \- 已取消
    """
    await update.message.reply_text(help_text, parse_mode=ParseMode.MARKDOWN_V2)


def extract_ids_from_text(text: str) -> List[str]:
    """从文本中提取所有验证ID"""
    ids = []
    parts = re.split(r'[\s\n]+', text.strip())
    
    for part in parts:
        if not part:
            continue
        try:
            vid = OneKeyClient.extract_verification_id(part)
            if vid and vid not in ids:
                ids.append(vid)
        except ValueError:
            continue
    
    return ids


async def verify_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /verify 命令"""
    if not context.args:
        await update.message.reply_text(
            "❌ 请提供验证链接或ID\n\n"
            "用法: /verify 链接或ID\n"
            "示例: /verify 6931007a35dfed1a6931adac",
        )
        return
    
    text = " ".join(context.args)
    verification_ids = extract_ids_from_text(text)
    
    if not verification_ids:
        await update.message.reply_text("❌ 无法从输入中提取有效的验证ID")
        return
    
    await process_verification(update, context, verification_ids)


async def batch_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /batch 命令"""
    if not context.args:
        await update.message.reply_text(
            "❌ 请提供验证链接或ID（最多5个）\n\n用法: /batch 链接1 链接2 ...",
        )
        return
    
    text = " ".join(context.args)
    verification_ids = extract_ids_from_text(text)
    
    if not verification_ids:
        await update.message.reply_text("❌ 无法从输入中提取有效的验证ID")
        return
    
    if len(verification_ids) > settings.max_batch_size:
        await update.message.reply_text(
            f"⚠️ 每批最多 {settings.max_batch_size} 个ID，您提供了 {len(verification_ids)} 个\n"
            f"将只处理前 {settings.max_batch_size} 个"
        )
        verification_ids = verification_ids[:settings.max_batch_size]
    
    await process_verification(update, context, verification_ids)


async def poll_single_status(
    vid: str,
    token: str,
    results: Dict[str, VerificationResult],
) -> tuple:
    """轮询单个验证状态，返回 (vid, new_token 或 None)"""
    try:
        status = await onekey_client.check_status(token)
        
        results[vid] = VerificationResult(
            verificationId=status.verification_id,
            currentStep=status.current_step,
            message=status.message,
            checkToken=status.check_token,
        )
        
        if status.current_step != VerificationStep.PENDING:
            return (vid, None)  # 完成
        elif status.check_token:
            return (vid, status.check_token)  # 继续轮询
        else:
            return (vid, None)
            
    except Exception as e:
        logger.error(f"Error polling status for {vid}: {e}")
        return (vid, None)


async def process_verification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    verification_ids: List[str],
):
    """处理验证请求 - 优化版：并发轮询"""
    user_id = update.effective_user.id
    
    # 检查去重
    duplicates = []
    unique_ids = []
    for vid in verification_ids:
        if await onekey_client.check_duplicate(vid):
            duplicates.append(vid)
        else:
            unique_ids.append(vid)
    
    if duplicates:
        await update.message.reply_text(
            f"⚠️ 以下ID正在处理中，已跳过: {', '.join(duplicates)}"
        )
    
    if not unique_ids:
        return
    
    verification_ids = unique_ids
    
    # 记录统计
    if stats_storage:
        await stats_storage.record_submission(user_id, len(verification_ids))
    
    # 发送处理中消息
    status_msg = await update.message.reply_text(
        f"🔄 开始验证 {len(verification_ids)} 个ID...\n\n"
        + "\n".join([f"⏳ {vid}" for vid in verification_ids]),
    )
    
    results: Dict[str, VerificationResult] = {}
    pending_tokens: Dict[str, str] = {}  # vid -> check_token
    last_update_time = 0
    update_interval = 1.5  # 消息更新最小间隔（秒）
    
    try:
        # 批量提交验证
        async for result in onekey_client.batch_verify(verification_ids):
            results[result.verification_id] = result
            
            if result.check_token and result.current_step == VerificationStep.PENDING:
                pending_tokens[result.verification_id] = result.check_token
            
            # 限制消息更新频率
            now = asyncio.get_event_loop().time()
            if now - last_update_time >= update_interval:
                await update_status_message(status_msg, verification_ids, results)
                last_update_time = now
        
        # 并发轮询 pending 状态
        poll_count = 0
        max_polls = settings.poll_max_attempts
        
        while pending_tokens and poll_count < max_polls:
            poll_count += 1
            await asyncio.sleep(settings.poll_interval)
            
            # 并发执行所有轮询
            tasks = [
 
