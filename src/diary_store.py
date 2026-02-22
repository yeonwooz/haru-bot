"""Notion 데이터베이스에 일기를 저장하는 모듈"""

import os

from notion_client import Client


def save_diary(date: str, summary: str, comment: str | None = None):
    """오늘의 일기를 Notion DB에 저장한다.

    Args:
        date: 날짜 (YYYY-MM-DD)
        summary: Claude가 생성한 오늘 한 일 요약
        comment: 사용자 코멘트
    """
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DIARY_DB_ID")

    if not token or not db_id:
        print("[Diary] NOTION_TOKEN 또는 NOTION_DIARY_DB_ID가 설정되지 않음 - 건너뜀")
        return False

    client = Client(auth=token)

    properties = {
        "summary": {"title": [{"text": {"content": summary[:2000]}}]},
        "date": {"date": {"start": date}},
    }

    if comment:
        properties["comment"] = {
            "rich_text": [{"text": {"content": comment[:2000]}}],
        }

    try:
        client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
        )
        print(f"[Diary] {date} 일기 Notion에 저장 완료")
        return True
    except Exception as e:
        print(f"[Diary] Notion 저장 실패: {e}")
        return False


def update_diary_comment(date: str, comment: str) -> bool:
    """기존 일기의 코멘트를 업데이트한다.

    Args:
        date: 날짜 (YYYY-MM-DD)
        comment: 사용자 코멘트
    """
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DIARY_DB_ID")

    if not token or not db_id:
        print("[Diary] NOTION_TOKEN 또는 NOTION_DIARY_DB_ID가 설정되지 않음 - 건너뜀")
        return False

    client = Client(auth=token)

    # 해당 날짜의 일기 찾기 (search API 사용)
    try:
        results = client.search(
            filter={"property": "object", "value": "page"},
        )
    except Exception as e:
        print(f"[Diary] Notion 검색 실패: {e}")
        return False

    db_id_clean = db_id.replace("-", "")
    page_id = None
    for page in results.get("results", []):
        parent = page.get("parent", {})
        if parent.get("database_id", "").replace("-", "") != db_id_clean:
            continue
        date_prop = page["properties"].get("date", {}).get("date")
        if date_prop and date_prop.get("start") == date:
            page_id = page["id"]
            break

    if not page_id:
        print(f"[Diary] {date} 일기를 찾을 수 없음")
        return False

    try:
        client.pages.update(
            page_id=page_id,
            properties={
                "comment": {
                    "rich_text": [{"text": {"content": comment[:2000]}}],
                },
            },
        )
        print(f"[Diary] {date} 일기에 코멘트 업데이트 완료")
        return True
    except Exception as e:
        print(f"[Diary] 코멘트 업데이트 실패: {e}")
        return False
