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
    admin_section = "
*管理员命令:*
• /stats \- 统计" if is_admin else ""
    await update.message.reply_text(f"🎓 *1Key Bot*
{admin_section}", parse_mode=ParseMode.MARKDOWN_V2)

def extract_ids_from_text(text: str) -> List[str]:
    ids = []
    parts = re.split(r'[\s
]+', text.strip())
    for part in parts:
        try:
            vid = OneKeyClient.extract_verification_id(part)
            if vid and vid not in ids: ids.append(vid)
        except: continue
    return ids

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
    with suppress(Exception): await msg.edit_text("
".join(lines))

async def handle_message(update, context):
    vids = extract_ids_from_text(update.message.text)
    if vids: await process_verification(update, context, vids[:settings.max_batch_size])

def main():
    global stats_storage
    stats_storage = create_stats_storage(settings.redis_url)
    app = Application.builder().token(settings.tg_bot_token).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.run_polling()

if __name__ == "__main__": main()