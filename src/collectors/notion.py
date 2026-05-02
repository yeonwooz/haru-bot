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


def collect_section_todos(
    page_query: str, section_title: str, model: str
) -> tuple[list[str], list[str]]:
    """page_query 페이지의 section_title 섹션에서 (오늘 미완료, 오늘 완료한) to-do를 반환한다.

    오늘 활성인 sub-section은 Claude가 헤더 이름(예: '주말에 할 일', '월요일 마다 할 일',
    '매일 아침')을 보고 자연어로 판단한다. 키워드 매칭 룰은 두지 않는다.
    'completed_today'는 활성 sub-section 안의 to-do 중 last_edited_time이 오늘 KST 날짜인 것.
    """
    token = os.environ.get("NOTION_TOKEN")
    if not token:
        return [], []
    client = Client(auth=token)

    page_id = _find_page_by_title(client, page_query)
    if not page_id:
        print(f"[Notion] '{page_query}' 페이지를 찾지 못함")
        return [], []

    blocks = _list_all_children(client, page_id)
    section_idx = _find_heading_index(blocks, section_title)
    if section_idx is None:
        print(f"[Notion] '{section_title}' heading을 찾지 못함")
        return [], []

    # sub-section 별로 to-do 수집. 각 to-do는 (text, checked, last_edited_iso)
    sections: list[tuple[str, list[tuple[str, bool, str]]]] = []
    current_heading: str | None = None
    current_todos: list[tuple[str, bool, str]] = []
    for b in blocks[section_idx + 1:]:
        bt = b.get("type", "")
        if bt in ("heading_1", "heading_2", "heading_3"):
            if current_heading is not None and current_todos:
                sections.append((current_heading, current_todos))
            heading_text = "".join(rt.get("plain_text", "") for rt in b[bt].get("rich_text", []))
            current_heading = heading_text.strip()
            current_todos = []
        elif bt == "to_do":
            text = "".join(rt.get("plain_text", "") for rt in b["to_do"].get("rich_text", [])).strip()
            if not text:
                continue
            checked = b["to_do"].get("checked", False)
            last_edited = b.get("last_edited_time", "")
            current_todos.append((text, checked, last_edited))
    if current_heading is not None and current_todos:
        sections.append((current_heading, current_todos))

    if not sections:
        return [], []

    today = datetime.now(KST)
    active_headings = _claude_pick_active_headings([h for h, _ in sections], today, model)

    uncompleted: list[str] = []
    completed_today: list[str] = []
    for heading, todos in sections:
        if heading not in active_headings:
            continue
        for text, checked, last_edited in todos:
            if not checked:
                uncompleted.append(text)
            elif _is_today_kst(last_edited, today):
                completed_today.append(text)

    print(
        f"[Notion] 활성 헤더 {len(active_headings)}개, "
        f"미완료 {len(uncompleted)}개, 오늘 완료 {len(completed_today)}개"
    )
    return uncompleted, completed_today


def _is_today_kst(iso_ts: str, today: datetime) -> bool:
    if not iso_ts:
        return False
    try:
        dt = datetime.fromisoformat(iso_ts.replace("Z", "+00:00"))
    except Exception:
        return False
    return dt.astimezone(KST).date() == today.date()


def _claude_pick_active_headings(headings: list[str], today: datetime, model: str) -> set[str]:
    """헤더 목록을 Claude에 보여주고 오늘 활성인 헤더 set만 받는다.

    ANTHROPIC_API_KEY가 없거나 호출 실패 시 모든 헤더를 활성으로 fallback.
    """
    valid: set[str] = set(headings)

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[Notion] ANTHROPIC_API_KEY 미설정 - 모든 헤더 활성")
        return valid

    import anthropic

    weekday_kr = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"][today.weekday()]
    today_str = today.strftime("%Y-%m-%d")
    headings_text = "\n".join(f"- {h}" for h in headings)

    user_prompt = (
        f"오늘은 {today_str} {weekday_kr}이야.\n\n"
        f"아래는 노션 페이지의 sub-section 헤더 목록이야. 이 중 오늘 활성인 헤더만 골라줘.\n\n"
        f"{headings_text}\n\n"
        "예: '주말에 할 일'은 토/일에만, '월요일 마다 할 일'은 월요일에만, "
        "'매월 1일에 할 일'은 매달 1일에만, 시간/날짜 조건이 없는 헤더(예: '매일 아침', '빠른 시일 내')는 항상 활성. "
        "응답은 활성 헤더 이름만 한 줄에 하나씩, 원본 그대로 출력해. 다른 설명·접두는 붙이지 마."
    )

    try:
        client = anthropic.Anthropic(api_key=api_key.strip())
        msg = client.messages.create(
            model=model,
            max_tokens=400,
            messages=[{"role": "user", "content": user_prompt}],
        )
        response = msg.content[0].text.strip()
    except Exception as e:
        print(f"[Notion] Claude 호출 실패: {e} - 모든 헤더 활성")
        return valid

    active: set[str] = set()
    for line in response.split("\n"):
        cleaned = line.strip().lstrip("-* ").strip()
        if cleaned in valid:
            active.add(cleaned)
    return active


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


def _find_heading_index(blocks: list[dict], target: str) -> int | None:
    for i, b in enumerate(blocks):
        bt = b.get("type", "")
        if bt in ("heading_1", "heading_2", "heading_3"):
            text = "".join(rt.get("plain_text", "") for rt in b[bt].get("rich_text", [])).strip()
            if text == target:
                return i
    return None


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
