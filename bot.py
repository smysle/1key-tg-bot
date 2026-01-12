"""
1Key Google 学生认证 Telegram Bot

命令:
/start - 开始使用
/help - 帮助信息
/verify <url或id> - 提交验证（支持多个，用空格或换行分隔）
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
from typing import List
from functools import wraps

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

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

# 全局统计存储
stats_storage: StatsStorage = None

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
        text = text.replace(char, f'\\{char}')
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
• /stats \\- 全局统计
• /stats24 \\- 24小时统计
• /user <id> \\- 查看用户统计
"""
    
    welcome_text = f"""
🎓 *1Key Google 学生认证 Bot*

欢迎使用！本 Bot 帮助您批量验证 Google 学生认证。

*使用方法:*
1️⃣ 发送 /verify <链接或ID> 开始验证
2️⃣ 或直接发送验证链接/ID（每行一个）
3️⃣ 使用 /batch 一次验证多个（最多5个）

*命令列表:*
• /verify \\- 提交单个或多个验证
• /batch \\- 批量验证
• /status \\- 查询验证状态
• /cancel \\- 取消验证
• /mystats \\- 个人统计
• /help \\- 查看帮助
{admin_section}
*Tips:*
• 支持直接粘贴 Google One 链接
• 支持直接粘贴 24位验证ID
• 每批最多 5 个验证ID
    """
    await update.message.reply_text(
        welcome_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /help 命令"""
    help_text = """
📖 *帮助信息*

*验证命令:*
`/verify <链接或ID>` \\- 验证单个
`/batch <链接1> <链接2> \\.\\.\\.` \\- 批量验证

*查询和管理:*
`/status <ID>` \\- 查询状态
`/cancel <ID>` \\- 取消验证
`/mystats` \\- 个人统计

*支持的输入格式:*
• 完整链接: `https://one\\.google\\.com/verify\\?\\.\\.\\.\\.`
• 验证ID: `6931007a35dfed1a6931adac`

*状态说明:*
⏳ pending \\- 处理中
✅ success \\- 成功
❌ error \\- 失败
🚫 cancelled \\- 已取消

*注意事项:*
• 每个 IP 只能使用一次
• 批量验证每批最多 5 个
• 验证过程可能需要几十秒
    """
    await update.message.reply_text(
        help_text,
        parse_mode=ParseMode.MARKDOWN_V2,
    )


def extract_ids_from_text(text: str) -> List[str]:
    """从文本中提取所有验证ID"""
    ids = []
    # 按行和空格分割
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
            "用法: `/verify <链接或ID>`\n"
            "示例: `/verify 6931007a35dfed1a6931adac`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    
    # 提取所有验证ID
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
            "❌ 请提供验证链接或ID（最多5个）\n\n"
            "用法: `/batch <链接1> <链接2> \\.\\.\\.`",
            parse_mode=ParseMode.MARKDOWN_V2,
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


async def process_verification(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    verification_ids: List[str],
):
    """处理验证请求"""
    user_id = update.effective_user.id
    
    # 记录统计
    if stats_storage:
        await stats_storage.record_submission(user_id, len(verification_ids))
    
    # 发送处理中消息
    status_msg = await update.message.reply_text(
        f"🔄 开始验证 {len(verification_ids)} 个ID...\n\n"
        + "\n".join([f"⏳ `{vid}`" for vid in verification_ids]),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    
    results = {}
    pending_tokens = {}  # vid -> check_token
    
    try:
        # 批量提交验证
        async for result in onekey_client.batch_verify(verification_ids):
            results[result.verification_id] = result
            
            if result.check_token:
                pending_tokens[result.verification_id] = result.check_token
            
            # 更新状态消息
            await update_status_message(status_msg, verification_ids, results)
        
        # 对于 pending 状态的，继续轮询
        while pending_tokens:
            await asyncio.sleep(3)  # 轮询间隔
            
            for vid, token in list(pending_tokens.items()):
                try:
                    status = await onekey_client.check_status(token)
                    
                    # 更新结果
                    results[vid] = VerificationResult(
                        verificationId=status.verification_id,
                        currentStep=status.current_step,
                        message=status.message,
                        checkToken=status.check_token,
                    )
                    
                    if status.current_step != VerificationStep.PENDING:
                        del pending_tokens[vid]
                    elif status.check_token:
                        pending_tokens[vid] = status.check_token
                    
                except Exception as e:
                    logger.error(f"Error polling status for {vid}: {e}")
                    del pending_tokens[vid]
            
            # 更新状态消息
            await update_status_message(status_msg, verification_ids, results)
        
        # 最终更新
        await update_status_message(status_msg, verification_ids, results, final=True)
        
    except OneKeyAPIError as e:
        await status_msg.edit_text(
            f"❌ API 错误: {escape_markdown(e.message)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.exception("Verification error")
        await status_msg.edit_text(f"❌ 验证出错: {escape_markdown(str(e))}", parse_mode=ParseMode.MARKDOWN_V2)


async def update_status_message(
    message,
    verification_ids: List[str],
    results: dict,
    final: bool = False,
):
    """更新状态消息"""
    lines = []
    
    if final:
        lines.append("📋 *验证完成*\n")
    else:
        lines.append("🔄 *验证中\\.\\.\\.*\n")
    
    for vid in verification_ids:
        result = results.get(vid)
        if result:
            emoji = STATUS_EMOJI.get(result.current_step, "❓")
            msg = escape_markdown(result.message[:50]) if result.message else ""
            lines.append(f"{emoji} `{vid}`")
            if msg:
                lines.append(f"   └ {msg}")
        else:
            lines.append(f"⏳ `{vid}`")
    
    try:
        await message.edit_text(
            "\n".join(lines),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        logger.warning(f"Failed to update message: {e}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /status 命令"""
    if not context.args:
        await update.message.reply_text(
            "❌ 请提供验证ID或 check token\n\n"
            "用法: `/status <ID>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    
    token = context.args[0]
    
    await update.message.reply_text("🔍 正在查询状态...")
    
    try:
        result = await onekey_client.check_status(token)
        emoji = STATUS_EMOJI.get(result.current_step, "❓")
        
        await update.message.reply_text(
            f"{emoji} *验证状态*\n\n"
            f"ID: `{escape_markdown(result.verification_id)}`\n"
            f"状态: {escape_markdown(result.current_step.value)}\n"
            f"消息: {escape_markdown(result.message)}",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    except OneKeyAPIError as e:
        await update.message.reply_text(f"❌ 查询失败: {e.message}")
    except Exception as e:
        await update.message.reply_text(f"❌ 查询出错: {str(e)}")


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /cancel 命令"""
    if not context.args:
        await update.message.reply_text(
            "❌ 请提供验证ID\n\n"
            "用法: `/cancel <ID>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    
    try:
        vid = OneKeyClient.extract_verification_id(context.args[0])
    except ValueError as e:
        await update.message.reply_text(f"❌ 无效的验证ID: {str(e)}")
        return
    
    await update.message.reply_text("🔄 正在取消验证...")
    
    try:
        result = await onekey_client.cancel_verification(vid)
        
        if result.already_cancelled:
            await update.message.reply_text(f"⚠️ 验证 `{vid}` 已经被取消过了", parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await update.message.reply_text(f"✅ 已取消验证 `{vid}`", parse_mode=ParseMode.MARKDOWN_V2)
            
    except OneKeyAPIError as e:
        await update.message.reply_text(f"❌ 取消失败: {e.message}")
    except Exception as e:
        await update.message.reply_text(f"❌ 取消出错: {str(e)}")


async def mystats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /mystats 命令 - 个人统计"""
    user_id = update.effective_user.id
    
    if not stats_storage:
        await update.message.reply_text("❌ 统计功能未启用")
        return
    
    user_stats = await stats_storage.get_user_stats(user_id)
    
    await update.message.reply_text(
        f"📊 *个人统计*\n\n"
        f"👤 用户ID: `{user_id}`\n"
        f"📝 总提交数: *{user_stats['total']}*\n"
        f"🕐 24小时提交: *{user_stats['last_24h']}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_required
async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stats 命令 - 全局统计（管理员）"""
    if not stats_storage:
        await update.message.reply_text("❌ 统计功能未启用")
        return
    
    all_stats = await stats_storage.get_all_stats()
    
    top_users_text = ""
    if all_stats['top_users']:
        top_users_text = "\n*Top 10 用户:*\n"
        for i, u in enumerate(all_stats['top_users'], 1):
            top_users_text += f"{i}\\. `{u['user_id']}` \\- {u['count']} 次\n"
    
    await update.message.reply_text(
        f"📊 *全局统计*\n\n"
        f"📝 总提交数: *{all_stats['total_submissions']}*\n"
        f"👥 总用户数: *{all_stats['total_users']}*\n"
        f"{top_users_text}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_required
async def stats24_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /stats24 命令 - 24小时统计（管理员）"""
    if not stats_storage:
        await update.message.reply_text("❌ 统计功能未启用")
        return
    
    stats_24h = await stats_storage.get_24h_stats()
    
    top_users_text = ""
    if stats_24h['top_users_24h']:
        top_users_text = "\n*24小时 Top 10:*\n"
        for i, u in enumerate(stats_24h['top_users_24h'], 1):
            top_users_text += f"{i}\\. `{u['user_id']}` \\- {u['count']} 次\n"
    
    await update.message.reply_text(
        f"🕐 *24小时统计*\n\n"
        f"📝 24h提交数: *{stats_24h['total_24h']}*\n"
        f"👥 24h活跃用户: *{stats_24h['users_24h']}*\n"
        f"{top_users_text}",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


@admin_required
async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理 /user <id> 命令 - 查看指定用户统计（管理员）"""
    if not context.args:
        await update.message.reply_text(
            "❌ 请提供用户ID\n\n用法: `/user <user_id>`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return
    
    try:
        target_user_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ 无效的用户ID")
        return
    
    if not stats_storage:
        await update.message.reply_text("❌ 统计功能未启用")
        return
    
    user_stats = await stats_storage.get_user_stats(target_user_id)
    
    await update.message.reply_text(
        f"📊 *用户统计*\n\n"
        f"👤 用户ID: `{target_user_id}`\n"
        f"📝 总提交数: *{user_stats['total']}*\n"
        f"🕐 24小时提交: *{user_stats['last_24h']}*",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """处理普通消息（自动识别验证链接/ID）"""
    text = update.message.text
    
    if not text:
        return
    
    # 尝试提取验证ID
    verification_ids = extract_ids_from_text(text)
    
    if verification_ids:
        if len(verification_ids) > settings.max_batch_size:
            await update.message.reply_text(
                f"⚠️ 检测到 {len(verification_ids)} 个验证ID，每批最多处理 {settings.max_batch_size} 个\n"
                f"将只处理前 {settings.max_batch_size} 个"
            )
            verification_ids = verification_ids[:settings.max_batch_size]
        
        await process_verification(update, context, verification_ids)


async def error_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """全局错误处理"""
    logger.error(f"Update {update} caused error {context.error}")
    
    if update and update.effective_message:
        await update.effective_message.reply_text(
            "❌ 发生了一个错误，请稍后重试"
        )


def main():
    """主函数"""
    global stats_storage
    
    # 初始化统计存储
    stats_storage = create_stats_storage(settings.redis_url)
    
    # 创建 Application
    application = (
        Application.builder()
        .token(settings.tg_bot_token)
        .build()
    )
    
    # 注册命令处理器
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("verify", verify_command))
    application.add_handler(CommandHandler("batch", batch_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("mystats", mystats_command))
    
    # 管理员命令
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("stats24", stats24_command))
    application.add_handler(CommandHandler("user", user_stats_command))
    
    # 处理普通消息（自动识别链接）
    application.add_handler(MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message,
    ))
    
    # 错误处理
    application.add_error_handler(error_handler)
    
    # 启动 Bot
    logger.info("Starting bot...")
    logger.info(f"Admin user IDs: {settings.admin_user_ids}")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
