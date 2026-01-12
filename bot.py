"""
1Key Google 学生认证 Telegram Bot
优化版: 并发轮询、优雅关闭、更好的错误处理
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

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

stats_storage = None
STATUS_EMOJI = {
    VerificationStep.UNKNOWN: "❓",
    VerificationStep.PENDING: "⏳",
    VerificationStep.SUCCESS: "✅",
    VerificationStep.ERROR: "❌",
    VerificationStep.CANCELLED: "🚫",
}

def escape_markdown(text: str) -> str:
    special_chars = ['_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']
    for char in special_chars: text = text.replace(char, f'\{char}')
    return text

def admin_required(func):
    @wraps(func)
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE, *args, **kwargs):
        if update.effective_user.id not in settings.admin_user_ids:
            await update.message.reply_text("❌ 管理员限定")
            return
        return await func(update, context, *args, **kwargs)
    return wrapper

async def start_command(update, context):
    is_admin = update.effective_user.id in settings.admin_user_ids
    admin_section = "\n*管理员命令:*\n• /stats \- 统计" if is_admin else ""
    await update.message.reply_text(f"🎓 *1Key Bot*\n{admin_section}", parse_mode=ParseMode.MARKDOWN_V2)

async def help_command(update, context):
    text = "*命令列表:*\n/verify \- 验证\n/batch \- 批量\n/status \- 状态\n/cancel \- 取消\n/mystats \- 个人统计"
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)

def extract_ids_from_text(text: str) -> List[str]:
    ids = []
    parts = re.split(r'[\s\n]+', text.strip())
    for part in parts:
        try:
            vid = OneKeyClient.extract_verification_id(part)
            if vid and vid not in ids: ids.append(vid)
        except: continue
    return ids

async def verify_command(update, context):
    if not context.args: return await update.message.reply_text("❌ 用法: /verify <ID>")
    vids = extract_ids_from_text(" ".join(context.args))
    if vids: await process_verification(update, context, vids)

async def batch_command(update, context):
    if not context.args: return await update.message.reply_text("❌ 用法: /batch <ID1> <ID2>...")
    vids = extract_ids_from_text(" ".join(context.args))
    if vids: await process_verification(update, context, vids[:settings.max_batch_size])

async def poll_single_status(vid: str, token: str, results: Dict[str, VerificationResult]) -> tuple:
    try:
        status = await onekey_client.check_status(token)
        results[vid] = VerificationResult(verificationId=status.verification_id, currentStep=status.current_step, message=status.message, checkToken=status.check_token)
        return (vid, status.check_token if status.current_step == VerificationStep.PENDING else None)
    except: return (vid, None)

async def process_verification(update, context, vids):
    user_id = update.effective_user.id
    unique_ids = [vid for vid in vids if not await onekey_client.check_duplicate(vid)]
    if not unique_ids:
        await update.message.reply_text("⚠️ 正在处理中")
        return
    if stats_storage: await stats_storage.record_submission(user_id, len(unique_ids))
    status_msg = await update.message.reply_text(f"🔄 处理 {len(unique_ids)} 个ID...")
    results, tokens = {}, {}
    try:
        async for res in onekey_client.batch_verify(unique_ids):
            results[res.verification_id] = res
            if res.check_token and res.current_step == VerificationStep.PENDING: tokens[res.verification_id] = res.check_token
            await update_status_message(status_msg, unique_ids, results)
        for _ in range(settings.poll_max_attempts):
            if not tokens: break
            await asyncio.sleep(settings.poll_interval)
            tasks = [poll_single_status(vid, t, results) for vid, t in tokens.items()]
            poll_res = await asyncio.gather(*tasks)
            tokens = {v: t for v, t in poll_res if t}
            await update_status_message(status_msg, unique_ids, results)
        await update_status_message(status_msg, unique_ids, results, final=True)
    except Exception as e:
        await status_msg.edit_text(f"❌ 错误: {e}")
    finally:
        for vid in unique_ids: await onekey_client.remove_pending(vid)

async def update_status_message(msg, vids, results, final=False):
    lines = ["📋 完成" if final else "🔄 处理中"]
    for v in vids:
        r = results.get(v)
        e = STATUS_EMOJI.get(r.current_step, "❓") if r else "⏳"
        m = f" - {r.message[:30]}" if r and r.message else ""
        lines.append(f"{e} {v}{m}")
    with suppress(Exception): await msg.edit_text("\n".join(lines))

async def status_command(update, context):
    if not context.args: return await update.message.reply_text("❌ 用法: /status <ID>")
    try:
        res = await onekey_client.check_status(context.args[0])
        await update.message.reply_text(f"状态: {res.current_step.value}\n消息: {res.message}")
    except Exception as e: await update.message.reply_text(f"❌ 错误: {e}")

async def cancel_command(update, context):
    if not context.args: return await update.message.reply_text("❌ 用法: /cancel <ID>")
    try:
        res = await onekey_client.cancel_verification(context.args[0])
        await update.message.reply_text("✅ 已取消" if not res.already_cancelled else "⚠️ 已取消过")
    except Exception as e: await update.message.reply_text(f"❌ 错误: {e}")

async def mystats_command(update, context):
    if not stats_storage: return
    s = await stats_storage.get_user_stats(update.effective_user.id)
    await update.message.reply_text(f"📊 统计\n总计: {s['total']}\n24h: {s['last_24h']}")

@admin_required
async def stats_command(update, context):
    if not stats_storage: return
    s = await stats_storage.get_all_stats()
    await update.message.reply_text(f"📊 总提交: {s['total_submissions']}\n总用户: {s['total_users']}")

async def handle_message(update, context):
    if not update.message or not update.message.text: return
    vids = extract_ids_from_text(update.message.text)
    if vids: await process_verification(update, context, vids[:settings.max_batch_size])

def main():
    global stats_storage
    stats_storage = create_stats_storage(settings.redis_url)
    app = Application.builder().token(settings.tg_bot_token).build()
    
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("verify", verify_command))
    app.add_handler(CommandHandler("batch", batch_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    app.add_handler(CommandHandler("mystats", mystats_command))
    app.add_handler(CommandHandler("stats", stats_command))
    
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("Bot started with all commands registered")
    app.run_polling()

if __name__ == "__main__": main()