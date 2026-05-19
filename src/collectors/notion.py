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

        is_todo = _is_in_todo_db(client, page, db_name_cache)
        done = True
        if is_todo:
            done = _has_checked_checkbox(page)

        title = _extract_title(page)
        tags = _extract_tags(page)
        excerpt = _extract_excerpt(client, page["id"])

        results.append({
            "title": title,
            "tags": tags,
            "excerpt": excerpt,
            "last_edited": last_edited_str,
            "done": done,
            "is_todo": is_todo,
        })

    done_count = sum(1 for r in results if r["done"])
    todo_count = sum(1 for r in results if not r["done"])
    print(f"[Notion] {len(results)}개 페이지 수집 완료 (완료 {done_count}, 미완료 {todo_count})")
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


def collect_today_daily_todo_page(today: datetime) -> tuple[list[str], list[str]]:
    """`YYYY-MM-DD TODO` 페이지(notion-daily-todo가 매일 만드는 페이지)에서 to-do 수집.

    페이지 안 모든 to_do 블록을 카테고리(heading_2) 무시하고:
    - unchecked → 미완료
    - checked  → 오늘 완료 (페이지 자체가 오늘 생성된 것이므로 체크는 모두 오늘로 간주)
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return [], []
    client = Client(auth=token)

    title_query = today.strftime("%Y-%m-%d") + " TODO"
    page_id = _find_page_by_title(client, title_query)
    if not page_id:
        print(f"[Notion-Daily] '{title_query}' 페이지를 찾지 못함")
        return [], []

    blocks = _list_all_children(client, page_id)
    uncompleted: list[str] = []
    completed: list[str] = []
    for b in blocks:
        if b.get("type") != "to_do":
            continue
        text = "".join(rt.get("plain_text", "") for rt in b["to_do"].get("rich_text", [])).strip()
        if not text:
            continue
        if b["to_do"].get("checked", False):
            completed.append(text)
        else:
            uncompleted.append(text)

    print(
        f"[Notion-Daily] '{title_query}' 미완료 {len(uncompleted)}개, "
        f"오늘 완료 {len(completed)}개"
    )
    return uncompleted, completed


def _find_page_by_title(client: Client, title_query: str) -> str | None:
    results = client.search(query=title_query, filter={"property": "object", "value": "page"})
    for r in results.get("results", []):
        for prop in r.get("properties", {}).values():
            if prop.get("type") == "title":
                title_parts = prop.get("title", [])
                actual = "".join(t.get("plain_text", "") for t in title_parts).strip()
                if actual == title_query.strip():
                    return r["id"]
    return None


def _list_all_children(client: Client, block_id: str) -> list[dict]:
    blocks: list[dict] = []
    cursor = None
    while True:
        resp = client.blocks.children.list(block_id=block_id, page_size=100, start_cursor=cursor)
        blocks.extend(resp.get("results", []))
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")
    return blocks


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
