"""Telegram Bot으로 일기 요약·체크리스트·임의 메시지 전송"""

import os
import asyncio

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


def send_message(text: str) -> bool:
    """Telegram으로 임의 메시지를 전송한다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        return False

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=text)

    try:
        asyncio.run(_send())
        return True
    except Exception as e:
        print(f"[Telegram] 메시지 전송 실패: {e}")
        return False


def send_checklist(items: list[str]) -> bool:
    """미완료 항목 체크리스트를 인라인 키보드와 함께 전송한다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or not items:
        return False

    lines = ["완료한 항목을 눌러주세요!", ""]
    keyboard = []
    for i, item in enumerate(items):
        lines.append(f"{i + 1}. {item}")
        short = item[:30] + "..." if len(item) > 30 else item
        keyboard.append([InlineKeyboardButton(f"{short} ✓", callback_data=f"check:{i}")])

    text = "\n".join(lines)

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    try:
        asyncio.run(_send())
        print(f"[Telegram] 체크리스트 전송 완료 ({len(items)}개 항목)")
        return True
    except Exception as e:
        print(f"[Telegram] 체크리스트 전송 실패: {e}")
        return False


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
    # ✓ 마크가 Markdown 파싱에 문제를 일으킬 수 있으므로 plain text로 전송

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=message)

    try:
        asyncio.run(_send())
        print("[Telegram] 요약 메시지 전송 완료")
        return True
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")
        return False
