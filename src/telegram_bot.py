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


def wait_for_reply(timeout: int = 21600) -> str | None:
    """Telegram에서 사용자의 코멘트를 대기한다.

    Args:
        timeout: 대기 시간 (초), 기본 6시간

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

        print(f"[Telegram] 코멘트 대기 중 (최대 {timeout // 3600}시간)...")

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


def get_latest_reply() -> str | None:
    """Telegram에서 미확인 메시지 중 가장 최근 텍스트를 가져온다.

    getUpdates API를 사용하여 처리되지 않은 메시지를 확인하고,
    확인 후 offset을 업데이트하여 다음 호출 시 중복 방지.

    Returns:
        가장 최근 코멘트 텍스트, 없으면 None
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return None

    async def _get():
        bot = Bot(token=token)
        updates = await bot.get_updates()

        reply = None
        max_update_id = None

        for update in updates:
            if (update.message
                    and update.message.text
                    and str(update.message.chat_id) == str(chat_id)):
                reply = update.message.text
            if max_update_id is None or update.update_id > max_update_id:
                max_update_id = update.update_id

        # offset 업데이트하여 처리 완료 표시
        if max_update_id is not None:
            await bot.get_updates(offset=max_update_id + 1)

        return reply

    try:
        result = asyncio.run(_get())
        if result:
            print(f"[Telegram] 미처리 코멘트 발견: {result[:50]}...")
        else:
            print("[Telegram] 미처리 코멘트 없음")
        return result
    except Exception as e:
        print(f"[Telegram] 미처리 코멘트 확인 오류: {e}")
        return None


async def _wait_until_stopped(app):
    """앱이 중지될 때까지 대기하는 헬퍼."""
    while app.running:
        await asyncio.sleep(0.5)
