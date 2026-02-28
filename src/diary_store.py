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


def _find_page(client: Client, db_id: str, date: str) -> str | None:
    """지정한 날짜의 일기 페이지 ID를 찾는다."""
    db_id_clean = db_id.replace("-", "")
    try:
        results = client.search(filter={"property": "object", "value": "page"})
    except Exception as e:
        print(f"[Diary] Notion 검색 실패: {e}")
        return None

    for page in results.get("results", []):
        parent = page.get("parent", {})
        if parent.get("database_id", "").replace("-", "") != db_id_clean:
            continue
        date_prop = page["properties"].get("date", {}).get("date")
        if date_prop and date_prop.get("start") == date:
            return page["id"]
    return None


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


def save_diary(date: str, summary: str, comment: str | None = None, setting: str | None = None):
    """오늘의 일기를 Notion DB에 저장한다.

    Args:
        date: 날짜 (YYYY-MM-DD)
        summary: Claude가 생성한 오늘 한 일 요약
        comment: 사용자 코멘트
        setting: 사용자 설정 (프롬프트 피드백)
    """
    client, db_id = _get_client_and_db()
    if not client:
        return False

    properties = {
        "summary": {"title": [{"text": {"content": summary[:2000]}}]},
        "date": {"date": {"start": date}},
    }

    if comment:
        properties["comment"] = {
            "rich_text": [{"text": {"content": comment[:2000]}}],
        }

    if setting:
        properties["setting"] = {
            "rich_text": [{"text": {"content": setting[:2000]}}],
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
    """기존 일기의 코멘트를 업데이트한다."""
    client, db_id = _get_client_and_db()
    if not client:
        return False

    page_id = _find_page(client, db_id, date)
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


def save_setting(date: str, setting: str) -> bool:
    """기존 일기의 setting 컬럼에 설정을 추가한다. 기존 설정이 있으면 이어 붙인다."""
    client, db_id = _get_client_and_db()
    if not client:
        return False

    page_id = _find_page(client, db_id, date)
    if not page_id:
        print(f"[Diary] {date} 일기를 찾을 수 없음 - 설정 저장 실패")
        return False

    try:
        # 기존 설정 읽기
        page = client.pages.retrieve(page_id=page_id)
        existing = ""
        rich_text = page["properties"].get("setting", {}).get("rich_text", [])
        if rich_text:
            existing = rich_text[0].get("text", {}).get("content", "")

        new_setting = f"{existing}\n{setting}" if existing else setting

        client.pages.update(
            page_id=page_id,
            properties={
                "setting": {
                    "rich_text": [{"text": {"content": new_setting[:2000]}}],
                },
            },
        )
        print(f"[Diary] 설정 저장 완료: {setting[:50]}")
        return True
    except Exception as e:
        print(f"[Diary] 설정 저장 실패: {e}")
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
