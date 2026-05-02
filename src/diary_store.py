"""Notion 데이터베이스에 일기를 저장하는 모듈"""

import os

from notion_client import Client


def _get_client_and_db() -> tuple[Client, str] | tuple[None, None]:
    """Notion 클라이언트와 DB ID를 반환한다."""
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DIARY_DB_ID")
    if not token or not db_id:
        print("[Diary] NOTION_TOKEN 또는 NOTION_DIARY_DB_ID가 설정되지 않음 - 건너뜀")
        return None, None
    return Client(auth=token), db_id


def ensure_setting_column():
    """Notion diary DB에 setting 컬럼이 없으면 추가한다."""
    client, db_id = _get_client_and_db()
    if not client:
        return
    try:
        db = client.databases.retrieve(database_id=db_id)
        if "setting" not in db["properties"]:
            client.databases.update(
                database_id=db_id,
                properties={"setting": {"rich_text": {}}},
            )
            print("[Diary] setting 컬럼 추가 완료")
    except Exception as e:
        print(f"[Diary] setting 컬럼 확인/추가 실패: {e}")


def save_diary(date: str, summary: str) -> bool:
    """오늘의 일기를 Notion DB에 저장한다."""
    client, db_id = _get_client_and_db()
    if not client:
        return False

    try:
        client.pages.create(
            parent={"database_id": db_id},
            properties={
                "summary": {"title": [{"text": {"content": summary[:2000]}}]},
                "date": {"date": {"start": date}},
            },
        )
        print(f"[Diary] {date} 일기 Notion에 저장 완료")
        return True
    except Exception as e:
        print(f"[Diary] Notion 저장 실패: {e}")
        return False


def load_settings() -> list[str]:
    """Notion diary DB에서 모든 사용자 설정을 가져온다."""
    client, db_id = _get_client_and_db()
    if not client:
        return []

    db_id_clean = db_id.replace("-", "")
    settings = []

    try:
        results = client.search(filter={"property": "object", "value": "page"})
        for page in results.get("results", []):
            parent = page.get("parent", {})
            if parent.get("database_id", "").replace("-", "") != db_id_clean:
                continue
            rich_text = page["properties"].get("setting", {}).get("rich_text", [])
            if rich_text:
                text = rich_text[0].get("text", {}).get("content", "").strip()
                if text:
                    settings.append(text)
    except Exception as e:
        print(f"[Diary] 설정 로드 실패: {e}")

    if settings:
        print(f"[Diary] 사용자 설정 {len(settings)}건 로드됨")
    return settings
