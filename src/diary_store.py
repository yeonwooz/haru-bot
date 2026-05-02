"""Notion 데이터베이스에 일기를 저장·갱신하는 모듈

데이터 모델 (Notion API 2025-09-03+: data_source 컨테이너):
- 일기 페이지 properties:
  - summary (title): 짧은 제목 (날짜 문자열)
  - date (date)
  - tasks (rich_text): "[ ] 항목" / "[x] 항목" 줄들
  - comment (rich_text): 사용자 답장 누적
  - setting (rich_text): /설정 명령 누적
  - feedback (rich_text): 봇이 완료 후 보낸 의견
- 페이지 본문(children): heading_2 + paragraph 블록으로 [오늘의 일정] / [태스크] 섹션 표시
"""

import os

from notion_client import Client


def _get_client_and_db() -> tuple[Client, str] | tuple[None, None]:
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DIARY_DB_ID")
    if not token or not db_id:
        print("[Diary] NOTION_TOKEN 또는 NOTION_DIARY_DB_ID가 설정되지 않음 - 건너뜀")
        return None, None
    return Client(auth=token), db_id


def _get_data_source_id(client: Client, db_id: str) -> str | None:
    db = client.databases.retrieve(database_id=db_id)
    sources = db.get("data_sources") or []
    return sources[0]["id"] if sources else None


def _ensure_column(prop_name: str, prop_def: dict | None = None) -> bool:
    """data_source의 properties에 prop_name이 없으면 추가한다."""
    client, db_id = _get_client_and_db()
    if not client:
        return False
    try:
        ds_id = _get_data_source_id(client, db_id)
        if not ds_id:
            print("[Diary] data_sources가 비어 있음")
            return False
        ds = client.data_sources.retrieve(data_source_id=ds_id)
        if prop_name in ds.get("properties", {}):
            return True
        client.data_sources.update(
            data_source_id=ds_id,
            properties={prop_name: prop_def or {"rich_text": {}}},
        )
        print(f"[Diary] {prop_name} 컬럼 추가 완료")
        return True
    except Exception as e:
        print(f"[Diary] {prop_name} 컬럼 확인/추가 실패: {e}")
        return False


def ensure_setting_column():
    _ensure_column("setting")


def ensure_tasks_column():
    _ensure_column("tasks")


def ensure_feedback_column():
    _ensure_column("feedback")


def _find_page(client: Client, db_id: str, date: str) -> dict | None:
    """지정한 날짜의 일기 페이지 객체를 찾는다."""
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
            return page
    return None


def find_today_page_id(date: str) -> str | None:
    """오늘 날짜 일기 페이지 ID. 없으면 None."""
    client, db_id = _get_client_and_db()
    if not client:
        return None
    page = _find_page(client, db_id, date)
    return page["id"] if page else None


def _summary_to_blocks(summary: str) -> list[dict]:
    """[오늘의 일정] / [태스크] 섹션 텍스트를 heading_2 + paragraph 블록 배열로 변환한다."""
    blocks: list[dict] = []
    section_lines: list[str] = []

    def flush():
        text = "\n".join(section_lines).strip("\n")
        if not text.strip():
            return
        for chunk in text.split("\n\n"):
            chunk = chunk.rstrip()
            if not chunk:
                continue
            blocks.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"type": "text", "text": {"content": chunk[:2000]}}],
                },
            })

    for line in summary.split("\n"):
        stripped = line.strip()
        if stripped in ("[오늘의 일정]", "[태스크]"):
            flush()
            section_lines = []
            blocks.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {
                    "rich_text": [{"type": "text", "text": {"content": stripped}}],
                },
            })
        else:
            section_lines.append(line)
    flush()
    return blocks


def _tasks_to_rich_text(tasks: list[str]) -> list[dict]:
    if not tasks:
        return []
    text = "\n".join(f"[ ] {t}" for t in tasks)
    return [{"text": {"content": text[:2000]}}]


def save_diary(date: str, summary: str, tasks: list[str] | None = None) -> str | None:
    """오늘의 일기 페이지를 만들거나 갱신하고 page_id를 반환한다.

    - 같은 날짜 페이지가 이미 있으면 properties(title, date, tasks)만 update.
      본문(children blocks)은 사용자가 직접 메모를 적었을 수 있으므로 건드리지 않는다.
    - 없으면 properties + children(heading_2/paragraph 블록)으로 새로 만든다.
    """
    client, db_id = _get_client_and_db()
    if not client:
        return None

    properties = {
        "summary": {"title": [{"text": {"content": date}}]},
        "date": {"date": {"start": date}},
    }
    if tasks:
        properties["tasks"] = {"rich_text": _tasks_to_rich_text(tasks)}

    existing = _find_page(client, db_id, date)
    if existing:
        try:
            client.pages.update(page_id=existing["id"], properties=properties)
            print(f"[Diary] {date} 일기 properties 갱신")
            return existing["id"]
        except Exception as e:
            print(f"[Diary] properties 갱신 실패: {e}")
            return existing["id"]

    children = _summary_to_blocks(summary)
    try:
        page = client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
            children=children,
        )
        print(f"[Diary] {date} 일기 Notion에 저장 완료")
        return page["id"]
    except Exception as e:
        print(f"[Diary] Notion 저장 실패: {e}")
        return None


def _read_tasks(client: Client, page_id: str) -> list[tuple[str, bool]]:
    page = client.pages.retrieve(page_id=page_id)
    rich = page["properties"].get("tasks", {}).get("rich_text", [])
    if not rich:
        return []
    text = rich[0].get("text", {}).get("content", "")
    items: list[tuple[str, bool]] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.startswith("[x] ") or line.startswith("[X] "):
            items.append((line[4:], True))
        elif line.startswith("[ ] "):
            items.append((line[4:], False))
    return items


def get_tasks(page_id: str) -> list[tuple[str, bool]]:
    """페이지의 tasks 컬럼을 (text, done) 리스트로 반환."""
    client, _ = _get_client_and_db()
    if not client:
        return []
    try:
        return _read_tasks(client, page_id)
    except Exception as e:
        print(f"[Diary] tasks 조회 실패: {e}")
        return []


def toggle_task(page_id: str, index: int) -> tuple[bool, list[tuple[str, bool]]] | None:
    """tasks의 index 항목을 토글하고 (new_state, all_tasks)를 반환. 실패 시 None."""
    client, _ = _get_client_and_db()
    if not client:
        return None
    try:
        items = _read_tasks(client, page_id)
        if index < 0 or index >= len(items):
            return None
        text, done = items[index]
        items[index] = (text, not done)

        new_text = "\n".join(f"[{'x' if d else ' '}] {t}" for t, d in items)
        client.pages.update(
            page_id=page_id,
            properties={
                "tasks": {"rich_text": [{"text": {"content": new_text[:2000]}}]},
            },
        )
        return (not done, items)
    except Exception as e:
        print(f"[Diary] toggle_task 실패: {e}")
        return None


def save_feedback(page_id: str, feedback: str) -> bool:
    client, _ = _get_client_and_db()
    if not client:
        return False
    try:
        client.pages.update(
            page_id=page_id,
            properties={
                "feedback": {"rich_text": [{"text": {"content": feedback[:2000]}}]},
            },
        )
        print(f"[Diary] feedback 저장 완료")
        return True
    except Exception as e:
        print(f"[Diary] feedback 저장 실패: {e}")
        return False


def append_comment(page_id: str, comment: str) -> bool:
    client, _ = _get_client_and_db()
    if not client:
        return False
    try:
        page = client.pages.retrieve(page_id=page_id)
        rich = page["properties"].get("comment", {}).get("rich_text", [])
        existing = rich[0].get("text", {}).get("content", "") if rich else ""
        new_text = f"{existing}\n{comment}" if existing else comment
        client.pages.update(
            page_id=page_id,
            properties={
                "comment": {"rich_text": [{"text": {"content": new_text[:2000]}}]},
            },
        )
        return True
    except Exception as e:
        print(f"[Diary] comment append 실패: {e}")
        return False


def append_setting(page_id: str, setting: str) -> bool:
    client, _ = _get_client_and_db()
    if not client:
        return False
    try:
        page = client.pages.retrieve(page_id=page_id)
        rich = page["properties"].get("setting", {}).get("rich_text", [])
        existing = rich[0].get("text", {}).get("content", "") if rich else ""
        new_text = f"{existing}\n{setting}" if existing else setting
        client.pages.update(
            page_id=page_id,
            properties={
                "setting": {"rich_text": [{"text": {"content": new_text[:2000]}}]},
            },
        )
        return True
    except Exception as e:
        print(f"[Diary] setting append 실패: {e}")
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
