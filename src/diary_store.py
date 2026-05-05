"""Notion 데이터베이스에 일기를 저장·갱신하는 모듈

데이터 모델 (Notion API 2025-09-03+: data_source 컨테이너):
- 일기 페이지 properties:
  - summary (title): 일정/완료한 태스크 요약 텍스트
  - date (date)
  - tasks (rich_text): "[ ] 항목" / "[x] 항목" 줄들
  - discussion (rich_text): "클로드: ..." / "나: ..." 줄로 누적되는 대화 로그
  - Status (select): "좋아!"/"별로"/"낫 배드?" 등 그날 기분
- 사용자 설정(/설정 명령으로 누적)은 일기 페이지가 아닌 별도 "설정 페이지"
  본문 블록으로 누적 (NOTION_SETTINGS_PAGE_ID).
"""

import os

from notion_client import Client

# 사용자가 /설정 X로 누적하는 봇 설정 전용 sub-page (노션 환경 specific)
# 부모: "📒 일기쓰기 - 나의 하루🍀" 페이지의 "⚙️ 설정" sub-page
NOTION_SETTINGS_PAGE_ID = "357bb67c-a90c-8166-bccd-d83857fa0e19"


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


def ensure_tasks_column():
    _ensure_column("tasks")


def ensure_discussion_column():
    _ensure_column("discussion")


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


def _tasks_to_rich_text(tasks: list[tuple[str, bool]]) -> list[dict]:
    if not tasks:
        return []
    text = "\n".join(f"[{'x' if done else ' '}] {t}" for t, done in tasks)
    return [{"text": {"content": text[:2000]}}]


def save_diary(date: str, summary: str, tasks: list[tuple[str, bool]] | None = None) -> str | None:
    """오늘의 일기 페이지를 만들거나 갱신하고 page_id를 반환한다.

    title(summary 컬럼)에 요약 텍스트 전체를 넣어 list view에서 한눈에 볼 수 있게 한다.
    같은 날짜 페이지가 있으면 properties만 update.
    """
    client, db_id = _get_client_and_db()
    if not client:
        return None

    properties = {
        "summary": {"title": [{"text": {"content": summary[:2000]}}]},
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

    try:
        page = client.pages.create(
            parent={"database_id": db_id},
            properties=properties,
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


def append_discussion(page_id: str, role: str, text: str) -> bool:
    """discussion 컬럼에 '{role}: {text}' 한 줄을 append한다 (2000자 컷)."""
    client, _ = _get_client_and_db()
    if not client:
        return False
    try:
        page = client.pages.retrieve(page_id=page_id)
        rich = page["properties"].get("discussion", {}).get("rich_text", [])
        existing = rich[0].get("text", {}).get("content", "") if rich else ""
        line = f"• {role}: {text}"
        new_text = f"{existing}\n{line}" if existing else line
        client.pages.update(
            page_id=page_id,
            properties={
                "discussion": {"rich_text": [{"text": {"content": new_text[:2000]}}]},
            },
        )
        return True
    except Exception as e:
        print(f"[Diary] discussion append 실패: {e}")
        return False


def bulletize_discussion() -> int:
    """기존 discussion 줄에 '• ' prefix를 붙인다 (일회성, 멱등).

    이미 '• '로 시작하는 줄이나 '클로드:'/'나:'로 시작하지 않는 줄은 건드리지 않는다.
    한 번 돌린 후 이 함수와 main.py 호출은 제거할 것.
    """
    client, db_id = _get_client_and_db()
    if not client:
        return 0

    db_id_clean = db_id.replace("-", "")
    try:
        results = client.search(filter={"property": "object", "value": "page"})
    except Exception as e:
        print(f"[Diary] bulletize 검색 실패: {e}")
        return 0

    converted = 0
    for page in results.get("results", []):
        parent = page.get("parent", {})
        if parent.get("database_id", "").replace("-", "") != db_id_clean:
            continue
        props = page.get("properties", {})
        rich = props.get("discussion", {}).get("rich_text", [])
        if not rich:
            continue
        existing = rich[0].get("text", {}).get("content", "")
        if not existing:
            continue

        new_lines: list[str] = []
        changed = False
        for ln in existing.split("\n"):
            if ln.startswith("• "):
                new_lines.append(ln)
            elif ln.startswith("클로드: ") or ln.startswith("나: "):
                new_lines.append(f"• {ln}")
                changed = True
            else:
                new_lines.append(ln)
        if not changed:
            continue

        new_text = "\n".join(new_lines)
        try:
            client.pages.update(
                page_id=page["id"],
                properties={
                    "discussion": {"rich_text": [{"text": {"content": new_text[:2000]}}]},
                },
            )
            converted += 1
            date_str = props.get("date", {}).get("date", {}).get("start", "?")
            print(f"[Diary] {date_str} discussion bullet 변환 완료")
        except Exception as e:
            print(f"[Diary] bullet 변환 실패: {e}")

    if converted:
        print(f"[Diary] 총 {converted}개 페이지 bullet 변환 완료")
    return converted


def load_settings() -> list[str]:
    """설정 페이지 본문의 paragraph/bulleted_list_item 블록을 텍스트 리스트로 반환한다."""
    client, _ = _get_client_and_db()
    if not client:
        return []

    settings: list[str] = []
    try:
        cursor = None
        while True:
            resp = client.blocks.children.list(
                block_id=NOTION_SETTINGS_PAGE_ID,
                page_size=100,
                start_cursor=cursor,
            )
            for b in resp.get("results", []):
                bt = b.get("type", "")
                if bt in ("paragraph", "bulleted_list_item", "numbered_list_item", "to_do"):
                    rich = b.get(bt, {}).get("rich_text", [])
                    text = "".join(rt.get("plain_text", "") for rt in rich).strip()
                    if text:
                        settings.append(text)
            if not resp.get("has_more"):
                break
            cursor = resp.get("next_cursor")
    except Exception as e:
        print(f"[Diary] 설정 페이지 로드 실패: {e}")

    if settings:
        print(f"[Diary] 설정 페이지에서 {len(settings)}건 로드")
    return settings
