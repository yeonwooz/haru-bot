"""아침 텔레그램 todo 알림

src/daily_todo.py가 오전 6시(KST)에 `YYYY-MM-DD TODO` 페이지를 생성한 직후
이 모듈의 run()을 호출해, 페이지를 카테고리별로 읽어 텔레그램으로 알려준다.
(구 notion-daily-todo 봇 통합 전에는 morning-todo.yml이 7시에 따로 실행했음)

단독 실행도 가능하며(uv run python src/morning_todo.py), Notion 쓰기 없이
읽기 + 텔레그램 발송만 한다.
"""

import os
import sys
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv

from src.collectors import collect_today_daily_todo_by_category
from src.telegram_bot import send_message


def _format_message(today: str, by_category: dict[str, list[tuple[str, bool]]]) -> str:
    """카테고리별 미완료 할 일을 텔레그램 텍스트로 구성한다."""
    lines = [f"🌅 오늘의 할 일 ({today})", "=" * 20]
    total = 0
    for category, items in by_category.items():
        pending = [text for text, checked in items if not checked]
        if not pending:
            continue
        if category:
            lines.append(f"\n[{category}]")
        for text in pending:
            lines.append(f"• {text}")
            total += 1
    if total == 0:
        lines.append("\n오늘 등록된 할 일이 없어요. 좋은 하루 보내세요! 🌿")
    return "\n".join(lines)


def run() -> None:
    load_dotenv()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    print(f"=== 아침 todo 알림 ({today}) ===")
    by_category = collect_today_daily_todo_by_category(now)
    message = _format_message(today, by_category)
    print(message)

    if send_message(message):
        print("[Telegram] 아침 todo 알림 전송 완료")
    else:
        print("[Telegram] 전송 실패 또는 토큰 미설정 - 건너뜀")


if __name__ == "__main__":
    run()
