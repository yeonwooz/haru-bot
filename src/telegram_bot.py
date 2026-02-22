"""Telegram Bot으로 일기 요약 전송 및 코멘트 수신"""

import os
import asyncio

from telegram import Bot, Update
from telegram.ext import ApplicationBuilder, MessageHandler, filters


def send_summary(summary: str) -> bool:
    """Telegram으로 오늘의 요약을 전송한다.

    Returns:
        성공 여부
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[Telegram] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않음 - 건너뜀")
        return False

    message = f"오늘 하루 정리\n{'=' * 20}\n\n{summary}\n\n---\n코멘트를 남겨주세요. 오늘 하루는 어땠나요?"

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=message, parse_mode="Markdown")

    try:
        asyncio.run(_send())
        print("[Telegram] 요약 메시지 전송 완료")
        return True
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")
        return False


def wait_for_reply(timeout: int = 3600) -> str | None:
    """Telegram에서 사용자의 코멘트를 대기한다.

    Args:
        timeout: 대기 시간 (초)

    Returns:
        사용자 코멘트 텍스트, 타임아웃 시 None
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[Telegram] 설정 누락 - 코멘트 수신 건너뜀")
        return None

    reply_text = None

    async def _poll():
        nonlocal reply_text
        app = ApplicationBuilder().token(token).build()

        async def handle_message(update: Update, context):
            nonlocal reply_text
            if str(update.effective_chat.id) == str(chat_id):
                reply_text = update.message.text
                app.stop_running()

        app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

        print(f"[Telegram] 코멘트 대기 중 ({timeout}초)...")

        async with app:
            await app.start()
            try:
                await asyncio.wait_for(
                    _wait_until_stopped(app),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                print("[Telegram] 코멘트 대기 시간 초과")
            finally:
                await app.stop()

    try:
        asyncio.run(_poll())
    except Exception as e:
        print(f"[Telegram] 코멘트 수신 오류: {e}")

    if reply_text:
        print(f"[Telegram] 코멘트 수신: {reply_text[:50]}...")
    return reply_text


async def _wait_until_stopped(app):
    """앱이 중지될 때까지 대기하는 헬퍼."""
    while app.running:
        await asyncio.sleep(0.5)
