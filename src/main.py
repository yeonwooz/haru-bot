"""하루봇 메인 파이프라인

매일 오후 8시(KST) 실행되어:
1. 미처리 답장 확인 → 이전 일기에 코멘트 업데이트
2. Calendar, Notion에서 오늘 활동을 수집
3. Claude API로 오늘 한 일 3가지 요약 생성
4. Telegram으로 요약 전송
5. 오늘 일기 Notion 저장
6. 답장 대기 (최대 6시간) → 오면 바로 Notion 업데이트
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
from src.collectors import collect_calendar, collect_notion
from src.summarizer import generate_summary
from src.telegram_bot import send_summary, wait_for_reply, get_latest_reply
from src.diary_store import save_diary, update_diary_comment


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

    # 1. 미처리 답장 확인
    print("--- 1단계: 미처리 답장 확인 ---")
    pending_reply = get_latest_reply()
    if pending_reply:
        update_diary_comment(yesterday, pending_reply)

    # 2. 데이터 수집
    print("\n--- 2단계: 데이터 수집 ---")
    calendar_data = collect_calendar(config.PERIOD_DAYS)
    notion_data = collect_notion(config.PERIOD_DAYS)

    total = len(calendar_data) + len(notion_data)
    print(f"\n총 {total}개 항목 수집 (Calendar: {len(calendar_data)}, Notion: {len(notion_data)})\n")

    # 3. 요약 생성
    print("--- 3단계: 오늘 한 일 요약 ---")
    summary, usage = generate_summary(
        calendar_data=calendar_data,
        notion_data=notion_data,
        model=config.CLAUDE_MODEL,
        max_tokens=config.MAX_TOKENS,
    )
    print(f"\n{summary}\n")

    # 4. Telegram 전송
    print("--- 4단계: Telegram 전송 ---")
    sent = send_summary(summary)

    # 5. 오늘 일기 저장 (코멘트 없이)
    print("\n--- 5단계: 일기 저장 ---")
    save_diary(today, summary)

    # 6. 답장 대기 (최대 6시간)
    if sent:
        print("\n--- 6단계: 답장 대기 ---")
        comment = wait_for_reply(timeout=config.TELEGRAM_REPLY_TIMEOUT)
        if comment:
            update_diary_comment(today, comment)

    # 7. 사용량 기록
    duration_sec = time.time() - start_time
    _log_usage(today, duration_sec, usage, config.CLAUDE_MODEL)

    print(f"\n=== 하루봇 완료! ===")


if __name__ == "__main__":
    run()
