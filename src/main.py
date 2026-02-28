"""하루봇 메인 파이프라인

매일 오후 8시(KST) 실행되어:
1. 미처리 답장 확인 → 이전 일기에 코멘트 업데이트
2. Calendar, Notion에서 오늘 활동을 수집
3. Claude API로 오늘 한 일 3가지 요약 생성
4. Telegram으로 요약 전송
5. 오늘 일기 Notion 저장
6. 답장 대기 (최대 5분) → 오면 바로 Notion 업데이트
"""

import sys
import os
import csv
import time
from datetime import datetime, timedelta, timezone

KST = timezone(timedelta(hours=9))

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
USAGE_LOG_PATH = os.path.join(PROJECT_ROOT, "usage_log.csv")
from dotenv import load_dotenv

import config
from src.collectors import collect_calendar, collect_notion, collect_github
from src.summarizer import generate_summary
from src.telegram_bot import send_summary, send_message, wait_for_reply, get_all_replies
from src.diary_store import save_diary, update_diary_comment, save_setting, load_settings, ensure_setting_column


def _parse_messages(messages: list[str]) -> tuple[list[str], list[str]]:
    """메시지 리스트를 일반 코멘트와 설정으로 분류한다.

    Returns:
        (comments, settings)
    """
    comments = []
    settings = []
    for msg in messages:
        stripped = msg.strip()
        for prefix in ("/설정 ", "/set "):
            if stripped.startswith(prefix):
                setting = stripped[len(prefix):].strip()
                if setting:
                    settings.append(setting)
                break
        else:
            if stripped not in ("/설정", "/set"):
                comments.append(msg)
    return comments, settings


def _calc_cost(usage: dict, model: str) -> float:
    """토큰 사용량으로 비용(USD)을 계산한다."""
    pricing = config.MODEL_PRICING.get(model, {"input": 3.0, "output": 15.0})
    input_cost = usage["input_tokens"] / 1_000_000 * pricing["input"]
    output_cost = usage["output_tokens"] / 1_000_000 * pricing["output"]
    return input_cost + output_cost


def _log_usage(run_date: str, duration_sec: float, usage: dict, model: str, note: str = ""):
    """실행 기록을 CSV에 누적 저장한다."""
    cost = _calc_cost(usage, model)

    write_header = not os.path.exists(USAGE_LOG_PATH)
    with open(USAGE_LOG_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if write_header:
            writer.writerow([
                "date", "model", "input_tokens", "output_tokens",
                "cost_usd", "duration_sec", "source", "note",
            ])
        writer.writerow([
            run_date, model, usage["input_tokens"], usage["output_tokens"],
            f"{cost:.4f}", f"{duration_sec:.1f}", "bot", note,
        ])

    print(f"[Usage] {model}: 입력 {usage['input_tokens']}토큰, 출력 {usage['output_tokens']}토큰, 비용 ${cost:.4f}")


def run():
    """전체 파이프라인을 실행한다."""
    load_dotenv()
    start_time = time.time()
    today = datetime.now(KST).strftime("%Y-%m-%d")
    yesterday = (datetime.now(KST) - timedelta(days=1)).strftime("%Y-%m-%d")

    print(f"=== 하루봇 실행 ({today}) ===\n")

    # 0. Notion DB에 setting 컬럼 확보
    ensure_setting_column()

    # 1. 미처리 답장 확인
    print("--- 1단계: 미처리 답장 확인 ---")
    pending_settings = []
    replies = get_all_replies(consume=False)
    if replies:
        comments, settings = _parse_messages(replies)
        pending_settings.extend(settings)

        ok = True
        if comments:
            comment_text = "\n".join(comments)
            if not update_diary_comment(yesterday, comment_text):
                ok = False

        for s in settings:
            send_message(f"설정 저장됨: {s}")

        if ok:
            get_all_replies(consume=True)

    # 2. 데이터 수집
    print("\n--- 2단계: 데이터 수집 ---")
    calendar_data = collect_calendar(config.PERIOD_DAYS)
    notion_data = collect_notion(config.PERIOD_DAYS)
    github_data = collect_github(config.PERIOD_DAYS)

    total = len(calendar_data) + len(notion_data) + len(github_data)
    print(f"\n총 {total}개 항목 수집 (Calendar: {len(calendar_data)}, Notion: {len(notion_data)}, GitHub: {len(github_data)})\n")

    # 3. 요약 생성 (사용자 설정 반영)
    print("--- 3단계: 오늘 한 일 요약 ---")
    saved_settings = load_settings()
    all_settings = saved_settings + pending_settings
    summary, usage = generate_summary(
        calendar_data=calendar_data,
        notion_data=notion_data,
        model=config.CLAUDE_MODEL,
        max_tokens=config.MAX_TOKENS,
        github_data=github_data,
        user_settings=all_settings if all_settings else None,
    )
    print(f"\n{summary}\n")

    # 4. Telegram 전송
    print("--- 4단계: Telegram 전송 ---")
    sent = send_summary(summary)

    # 5. 오늘 일기 저장 (대기 중 받은 설정 포함)
    print("\n--- 5단계: 일기 저장 ---")
    setting_text = "\n".join(pending_settings) if pending_settings else None
    save_diary(today, summary, setting=setting_text)

    # 6. 답장 확인 및 대기
    if sent:
        print("\n--- 6단계: 답장 대기 ---")
        replies = get_all_replies()
        if not replies:
            reply = wait_for_reply(timeout=config.TELEGRAM_REPLY_TIMEOUT)
            replies = [reply] if reply else []

        if replies:
            comments, settings = _parse_messages(replies)
            if comments:
                update_diary_comment(today, "\n".join(comments))
            for s in settings:
                save_setting(today, s)
                send_message(f"설정 저장됨: {s}")

    # 7. 사용량 기록
    duration_sec = time.time() - start_time
    _log_usage(today, duration_sec, usage, config.CLAUDE_MODEL)

    print(f"\n=== 하루봇 완료! ===")


if __name__ == "__main__":
    run()
