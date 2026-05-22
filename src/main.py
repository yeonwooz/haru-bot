"""하루봇 메인 파이프라인

매일 오후 8시(KST) 실행되어:
1. Calendar / Notion / GitHub에서 오늘 활동 수집
2. Claude API로 [오늘의 일정] + [태스크] 두 섹션 요약 생성
3. Notion 일기 페이지 생성 (본문은 heading_2 + paragraph 블록, tasks는 [ ] 체크박스 rich_text)
4. Telegram에 일정 요약(텍스트) + 태스크 토글 키보드(미완료 항목 + 🏁 완료 버튼) 전송
5. 사용자가 🏁 완료를 누르면 webhook이 피드백 생성·저장 (api/webhook.py)

사용자 답장(코멘트/설정)은 Vercel webhook이 비동기로 받아 직접 Notion에 반영한다.
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
from src.collectors import (
    collect_calendar,
    collect_notion,
    collect_today_daily_todo_page,
    collect_github,
)
from src.summarizer import generate_summary, dedupe_tasks
from src.telegram_bot import send_summary, send_task_keyboard
from src.diary_store import (
    save_diary,
    load_settings,
    ensure_tasks_column,
    ensure_discussion_column,
    bulletize_discussion,
)


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


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in items:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _collect_uncompleted_tasks(calendar_data: list[dict], notion_data: list[dict]) -> list[str]:
    """버튼으로 토글할 미완료 태스크 raw 목록 (캘린더 + 노션 할일 중 done=false)."""
    tasks: list[str] = []
    for item in calendar_data:
        if not item.get("done"):
            tasks.append(item["summary"])
    for item in notion_data:
        if not item.get("done"):
            tasks.append(item["title"])
    return tasks


def run():
    """전체 파이프라인을 실행한다."""
    load_dotenv()
    start_time = time.time()
    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")

    print(f"=== 하루봇 실행 ({today}) ===\n")

    # 0. Notion DB 컬럼 확보 (setting 컬럼은 별도 설정 페이지로 이전됨)
    ensure_tasks_column()
    ensure_discussion_column()
    # TODO: 한 번 돌린 후 이 호출과 diary_store.bulletize_discussion 함수 제거
    bulletize_discussion()

    # 1. 데이터 수집
    print("--- 1단계: 데이터 수집 ---")
    calendar_data = collect_calendar(config.PERIOD_DAYS)
    notion_data = collect_notion(config.PERIOD_DAYS)
    github_data = collect_github(config.PERIOD_DAYS)
    todo_uncompleted, todo_completed = collect_today_daily_todo_page(now)

    total = len(calendar_data) + len(notion_data) + len(github_data)
    print(
        f"\n총 {total}개 항목 수집 (Calendar: {len(calendar_data)}, Notion: {len(notion_data)}, "
        f"GitHub: {len(github_data)}), 미완료 {len(todo_uncompleted)}, 오늘완료 {len(todo_completed)}\n"
    )

    # 2. 요약 생성 (캘린더 일정이 있거나 오늘 완료한 태스크가 있으면 Claude 호출)
    print("--- 2단계: 오늘 하루 정리 ---")
    if calendar_data or todo_completed:
        saved_settings = load_settings()
        summary, usage = generate_summary(
            calendar_data=calendar_data,
            notion_data=notion_data,
            model=config.CLAUDE_MODEL,
            max_tokens=config.MAX_TOKENS,
            github_data=github_data,
            user_settings=saved_settings if saved_settings else None,
            completed_today=todo_completed,
        )
        print(f"\n{summary}\n")
    else:
        summary = "일정 없음"
        usage = {"input_tokens": 0, "output_tokens": 0}
        print("[Summarizer] 캘린더 일정 0개 + 오늘 완료 태스크 0개 - Claude 호출 생략")

    uncompleted_tasks = _dedupe(
        _collect_uncompleted_tasks(calendar_data, notion_data) + todo_uncompleted
    )

    # 의미상 중복 ("회의 준비" vs "회의 준비하기") 제거 — 일기·텔레그램 양쪽에 반영
    uncompleted_tasks, dedupe_usage = dedupe_tasks(uncompleted_tasks, config.CLAUDE_MODEL)
    usage["input_tokens"] += dedupe_usage["input_tokens"]
    usage["output_tokens"] += dedupe_usage["output_tokens"]

    # tasks 컬럼에 미완료([ ])와 오늘 완료([x])를 함께 저장
    tasks_for_diary = [(t, False) for t in uncompleted_tasks] + [(t, True) for t in todo_completed]

    # 3. 일기 저장 (page_id 확보)
    print("--- 3단계: 일기 저장 ---")
    page_id = save_diary(today, summary, tasks=tasks_for_diary)

    # 4. Telegram 전송
    print("--- 4단계: Telegram 전송 ---")
    if calendar_data:
        send_summary(summary)
    else:
        print("[Telegram] 캘린더 일정 0개 - 일정 요약 메시지 생략")
    if page_id and uncompleted_tasks:
        send_task_keyboard(page_id, uncompleted_tasks)
    elif not uncompleted_tasks:
        print("[Telegram] 미완료 태스크 없음 - 태스크 키보드 생략")

    # 5. 사용량 기록
    duration_sec = time.time() - start_time
    _log_usage(today, duration_sec, usage, config.CLAUDE_MODEL)

    print(f"\n=== 하루봇 완료! ===")


if __name__ == "__main__":
    run()
