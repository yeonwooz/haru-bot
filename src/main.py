"""하루봇 메인 파이프라인

매일 오후 8시(KST) 실행되어:
1. Calendar, Notion, GitHub에서 오늘 활동 수집
2. Claude API로 오늘 한 일 요약 생성 (저장된 사용자 설정 반영)
3. Telegram으로 요약 + 미완료 체크리스트 전송
4. 오늘 일기 Notion 저장

사용자 답장(코멘트/설정)은 Vercel webhook(api/webhook.py)이 비동기로 받아 직접 Notion에 반영한다.
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
from src.telegram_bot import send_summary, send_checklist
from src.diary_store import save_diary, load_settings, ensure_setting_column


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

    print(f"=== 하루봇 실행 ({today}) ===\n")

    # 0. Notion DB에 setting 컬럼 확보
    ensure_setting_column()

    # 1. 데이터 수집
    print("--- 1단계: 데이터 수집 ---")
    calendar_data = collect_calendar(config.PERIOD_DAYS)
    notion_data = collect_notion(config.PERIOD_DAYS)
    github_data = collect_github(config.PERIOD_DAYS)

    total = len(calendar_data) + len(notion_data) + len(github_data)
    print(f"\n총 {total}개 항목 수집 (Calendar: {len(calendar_data)}, Notion: {len(notion_data)}, GitHub: {len(github_data)})\n")

    # 2. 요약 생성 (사용자 설정 반영)
    print("--- 2단계: 오늘 하루 정리 ---")
    saved_settings = load_settings()
    summary, usage = generate_summary(
        calendar_data=calendar_data,
        notion_data=notion_data,
        model=config.CLAUDE_MODEL,
        max_tokens=config.MAX_TOKENS,
        github_data=github_data,
        user_settings=saved_settings if saved_settings else None,
    )
    print(f"\n{summary}\n")

    # 3. Telegram 전송
    print("--- 3단계: Telegram 전송 ---")
    sent = send_summary(summary)

    # 3-1. 미완료 항목 체크리스트 전송
    if sent:
        uncompleted = []
        for item in calendar_data:
            if not item.get("done"):
                uncompleted.append(item["summary"])
        for item in notion_data:
            if not item.get("done"):
                uncompleted.append(item["title"])
        if uncompleted:
            send_checklist(uncompleted)

    # 4. 오늘 일기 저장
    print("\n--- 4단계: 일기 저장 ---")
    save_diary(today, summary)

    # 5. 사용량 기록
    duration_sec = time.time() - start_time
    _log_usage(today, duration_sec, usage, config.CLAUDE_MODEL)

    print(f"\n=== 하루봇 완료! ===")


if __name__ == "__main__":
    run()
