"""Telegram Bot으로 일정 요약·태스크 버튼 키보드·임의 메시지 전송"""

import os
import asyncio

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup


def _short_pid(page_id: str) -> str:
    return page_id.replace("-", "")


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


def send_summary(schedule_text: str) -> bool:
    """오늘의 일정 요약을 Telegram으로 전송한다."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("[Telegram] TELEGRAM_BOT_TOKEN 또는 TELEGRAM_CHAT_ID가 설정되지 않음 - 건너뜀")
        return False

    message = f"오늘 하루 정리\n{'=' * 20}\n\n{schedule_text}".rstrip()

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(chat_id=chat_id, text=message)

    try:
        asyncio.run(_send())
        print("[Telegram] 일정 요약 전송 완료")
        return True
    except Exception as e:
        print(f"[Telegram] 전송 실패: {e}")
        return False


def send_task_keyboard(page_id: str, tasks: list[str]) -> bool:
    """태스크 토글 버튼 + 🏁 완료 버튼 메시지를 전송한다.

    콜백 데이터 형식:
    - 토글: t:{page_id_short}:{index}
    - 완료: done:{page_id_short}
    """
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat_id or not tasks:
        return False

    pid = _short_pid(page_id)
    keyboard: list[list[InlineKeyboardButton]] = []
    for i, t in enumerate(tasks):
        label = t[:40] + "..." if len(t) > 40 else t
        keyboard.append([
            InlineKeyboardButton(f"☐ {label}", callback_data=f"t:{pid}:{i}"),
        ])
    keyboard.append([InlineKeyboardButton("🏁 완료", callback_data=f"done:{pid}")])

    text = "오늘 한 태스크를 체크해주세요. 다 끝나면 🏁 완료를 눌러주세요."

    async def _send():
        bot = Bot(token=token)
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    try:
        asyncio.run(_send())
        print(f"[Telegram] 태스크 키보드 전송 완료 ({len(tasks)}개 항목)")
        return True
    except Exception as e:
        print(f"[Telegram] 태스크 키보드 전송 실패: {e}")
        return False
