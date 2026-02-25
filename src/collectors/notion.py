"""Notion에서 최근 활동 데이터를 수집하는 모듈"""

import os
from datetime import datetime, timedelta, timezone
from notion_client import Client

KST = timezone(timedelta(hours=9))


def collect_notion(period_days: int) -> list[dict]:
    """Notion 워크스페이스 전체에서 최근 수정된 페이지를 검색하여 수집한다.

    Args:
        period_days: 수집할 기간 (일 단위)

    Returns:
        [{"title": str, "tags": list[str], "excerpt": str, "last_edited": str}, ...]
    """
    token = os.environ.get("NOTION_TOKEN")

    if not token:
        print("[Notion] NOTION_TOKEN이 설정되지 않음 - 건너뜀")
        return []

    client = Client(auth=token)
    since = datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)

    try:
        response = client.search(
            filter={"property": "object", "value": "page"},
            sort={"direction": "descending", "timestamp": "last_edited_time"},
            page_size=50,
        )
    except Exception as e:
        print(f"[Notion] API 호출 실패: {e}")
        return []

    db_name_cache = {}
    results = []
    skipped_todo = 0
    for page in response.get("results", []):
        last_edited_str = page.get("last_edited_time", "")
        if not last_edited_str:
            continue

        last_edited = datetime.fromisoformat(last_edited_str.replace("Z", "+00:00"))
        if last_edited < since:
            continue

        # "할일" DB 페이지는 체크박스가 체크된 것만 포함
        if _is_in_todo_db(client, page, db_name_cache):
            if not _has_checked_checkbox(page):
                skipped_todo += 1
                continue

        title = _extract_title(page)
        tags = _extract_tags(page)
        excerpt = _extract_excerpt(client, page["id"])

        results.append({
            "title": title,
            "tags": tags,
            "excerpt": excerpt,
            "last_edited": last_edited_str,
        })

    print(f"[Notion] {len(results)}개 페이지 수집 완료 (미완료 할일 {skipped_todo}개 제외)")
    return results


def _is_in_todo_db(client: Client, page: dict, cache: dict) -> bool:
    """페이지가 이름에 '할일'이 포함된 DB에 속하는지 확인한다."""
    parent = page.get("parent", {})
    if parent.get("type") != "database_id":
        return False

    db_id = parent["database_id"]
    if db_id not in cache:
        try:
            db = client.databases.retrieve(database_id=db_id)
            title_parts = db.get("title", [])
            cache[db_id] = "".join(t.get("plain_text", "") for t in title_parts)
        except Exception:
            cache[db_id] = ""

    return "할일" in cache[db_id]


def _has_checked_checkbox(page: dict) -> bool:
    """페이지의 checkbox 속성 중 하나라도 체크되어 있는지 확인한다."""
    for prop in page.get("properties", {}).values():
        if prop.get("type") == "checkbox" and prop.get("checkbox") is True:
            return True
    return False


def _extract_title(page: dict) -> str:
    """페이지에서 제목을 추출한다."""
    properties = page.get("properties", {})
    for prop in properties.values():
        if prop.get("type") == "title":
            title_parts = prop.get("title", [])
            return "".join(t.get("plain_text", "") for t in title_parts)
    return "제목 없음"


def _extract_tags(page: dict) -> list[str]:
    """페이지에서 태그(multi_select 또는 select)를 추출한다."""
    properties = page.get("properties", {})
    tags = []
    for prop in properties.values():
        if prop.get("type") == "multi_select":
            tags.extend(opt["name"] for opt in prop.get("multi_select", []))
        elif prop.get("type") == "select" and prop.get("select"):
            tags.append(prop["select"]["name"])
    return tags


def _extract_excerpt(client: Client, page_id: str, max_length: int = 300) -> str:
    """페이지 본문의 첫 부분을 텍스트로 추출한다."""
    try:
        blocks = client.blocks.children.list(block_id=page_id, page_size=10)
        texts = []
        for block in blocks.get("results", []):
            block_type = block.get("type", "")
            block_data = block.get(block_type, {})
            rich_texts = block_data.get("rich_text", [])
            for rt in rich_texts:
                texts.append(rt.get("plain_text", ""))
        full_text = " ".join(texts)
        return full_text[:max_length]
    except Exception:
        return ""
