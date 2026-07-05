"""매일 오전 6시(KST) Notion 체크박스 TODO 페이지 생성 + 아침 텔레그램 알림

(구 notion-daily-todo repo에서 이식 — 2026-07-04 haru-bot으로 통합.
 페이지 생성 직후 src/morning_todo.py의 알림을 바로 발송한다.)

페이지 계층:
  ROOT_PAGE_ID ("할일")
    └── {YYYY} 할일  (ensure_year_page가 매년 자동 생성)
          ├── # 할 일  (사용자가 채워두는 yearly 반복 패턴)
          ├── YYYY-MM (월별 sub-page, ensure_month_page가 자동 생성)
          │     └── YYYY-MM-DD TODO (매일 페이지)

콘텐츠 소스 (모두 "오늘의 할일"만 — 전날 미완료 이월은 하지 않는다):
1. 템플릿 페이지 (TEMPLATE_PAGE_ID) — 기본 카테고리 + 고정 할 일
2. {YYYY} 할일 페이지의 "# 할 일" 섹션 — 반복 패턴(매일/요일/날짜지정/빠른시일 등)
3. iCloud 캘린더 — 오늘(KST) 일정
4. 전날 TODO 페이지의 "🔜 내일로" 섹션 — webhook에서 사용자가 미룬 항목

2, 3 항목은 Claude API(config.CLAUDE_MODEL)가 템플릿 카테고리(일/생활/공부)로 분류한다.

사용법:
  uv run python src/daily_todo.py            # 페이지 생성 + 알림
  uv run python src/daily_todo.py --dry-run  # Notion 쓰기/텔레그램 없이 결과만 출력
"""

import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import caldav
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

load_dotenv()

import config
from src import llm

# 분류/필터도 일기 파이프라인과 같은 모델 사용 (호출량이 적어 비용 차이 미미,
# sonnet은 분류 누락이 잦아 opus로 통일 — 2026-07-04)
CLASSIFY_MODEL = config.CLAUDE_MODEL

NOTION_TOKEN = os.environ["NOTION_TOKEN"]
ROOT_PAGE_ID = os.environ["ROOT_PAGE_ID"]
TEMPLATE_PAGE_ID = os.environ["TEMPLATE_PAGE_ID"]
APPLE_ID = os.environ.get("APPLE_ID", "")
APPLE_APP_PASSWORD = os.environ.get("APPLE_APP_PASSWORD", "")

KST = timezone(timedelta(hours=9))
API_BASE = "https://api.notion.com/v1"
HEADERS = {
    "Authorization": f"Bearer {NOTION_TOKEN}",
    "Content-Type": "application/json; charset=utf-8",
    "Notion-Version": "2022-06-28",
}

WEEKDAY_KO = ["월요일", "화요일", "수요일", "목요일", "금요일", "토요일", "일요일"]


# ─────────────────────────── Notion API ───────────────────────────


def api_request(method: str, path: str, body: dict | None = None) -> dict:
    data = json.dumps(body).encode("utf-8") if body else None
    req = urllib.request.Request(
        f"{API_BASE}{path}", data=data, headers=HEADERS, method=method,
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def get_blocks(page_id: str) -> list[dict]:
    """페이지의 모든 블록을 가져온다 (paginated)."""
    blocks: list[dict] = []
    cursor: str | None = None
    while True:
        path = f"/blocks/{page_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        result = api_request("GET", path)
        blocks.extend(result.get("results", []))
        if not result.get("has_more"):
            break
        cursor = result.get("next_cursor")
    return blocks


def block_text(block: dict) -> str:
    btype = block.get("type")
    if btype not in block:
        return ""
    return "".join(t.get("plain_text", "") for t in block[btype].get("rich_text", []))


def find_page_by_title(title: str) -> str | None:
    """제목이 정확히 일치하는 페이지 id를 검색한다 (없으면 None)."""
    body = {
        "query": title,
        "filter": {"value": "page", "property": "object"},
        "page_size": 5,
    }
    result = api_request("POST", "/search", body)
    for page in result.get("results", []):
        title_parts = page.get("properties", {}).get("title", {}).get("title", [])
        if "".join(t.get("plain_text", "") for t in title_parts).strip() == title:
            return page["id"]
    return None


# webhook(api/webhook.py)이 "내일로 미루기"로 고른 항목을 그날 TODO 페이지의 이 heading
# 아래 모아둔다. 다음 아침 생성 시 전날 페이지의 이 섹션만 읽어 오늘로 가져온다 (선택적 이월).
DEFER_HEADING_KEY = "내일로"


def collect_deferred_from_yesterday(yesterday: str) -> list[str]:
    """전날 TODO 페이지의 '🔜 내일로' 섹션 to_do 텍스트(사용자가 내일로 미룬 항목)."""
    page_id = find_page_by_title(f"{yesterday} TODO")
    if not page_id:
        return []
    items: list[str] = []
    in_defer = False
    for b in get_blocks(page_id):
        btype = b.get("type")
        if btype == "heading_2":
            in_defer = DEFER_HEADING_KEY in block_text(b)
        elif btype == "to_do" and in_defer:
            t = block_text(b).strip()
            if t:
                items.append(t)
    return items


def blocks_to_children(blocks: list[dict]) -> list[dict]:
    """템플릿 블록을 새 페이지용 children으로 변환한다."""
    children = []
    for b in blocks:
        btype = b.get("type")
        if btype == "heading_2":
            children.append({
                "object": "block", "type": "heading_2",
                "heading_2": {"rich_text": b["heading_2"]["rich_text"]},
            })
        elif btype == "to_do":
            children.append({
                "object": "block", "type": "to_do",
                "to_do": {
                    "rich_text": b["to_do"]["rich_text"],
                    "checked": False,
                },
            })
        elif btype == "divider":
            children.append({"object": "block", "type": "divider", "divider": {}})
    return children


def to_do_block(text: str) -> dict:
    return {
        "object": "block", "type": "to_do",
        "to_do": {
            "rich_text": [{"text": {"content": text}}],
            "checked": False,
        },
    }


def template_categories(template_children: list[dict]) -> list[str]:
    """템플릿의 heading_2 텍스트(카테고리) 목록을 순서대로 반환."""
    result = []
    for b in template_children:
        if b.get("type") == "heading_2":
            text = "".join(t.get("plain_text", "") for t in b["heading_2"]["rich_text"])
            if text:
                result.append(text)
    return result


# ─────────────────── 연간 할일 페이지의 반복 패턴 파서 ───────────────────


def heading_matches_today(heading: str, today: datetime) -> bool:
    """heading이 오늘 날짜에 해당하는 반복 패턴이면 True."""
    h = heading.strip()
    weekday_idx = today.weekday()  # Mon=0
    weekday_name = WEEKDAY_KO[weekday_idx]
    is_weekend = weekday_idx >= 5

    # "매일 ... (주말 제외)" / "평일만" 등 주말 제외 명시가 있으면 주말엔 매칭 안 함
    excludes_weekend = "주말 제외" in h or "주말제외" in h or "평일만" in h

    if "매일" in h:
        return not (is_weekend and excludes_weekend)
    if weekday_name in h and "마다" in h:
        return True
    if "주말" in h and not excludes_weekend and is_weekend:
        return True
    if "평일" in h and not is_weekend:
        return True

    m = re.search(r"매월\s*(\d{1,2})\s*일", h)
    if m and int(m.group(1)) == today.day:
        return True

    # YYYY-MM-DD 또는 YYYY.M.D
    m = re.search(r"(\d{4})[\-.](\d{1,2})[\-.](\d{1,2})", h)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        if (y, mo, d) == (today.year, today.month, today.day):
            return True

    return False


def is_urgent_pool_heading(heading: str) -> bool:
    return "빠른 시일" in heading or "빠른시일" in heading


def parse_recurring_todos(
    blocks: list[dict], today: datetime,
) -> tuple[list[dict], list[str]]:
    """연간 할일 페이지의 "# 할 일" 섹션 이하를 파싱.

    Returns:
        recurring: [{"section": "매일 아침 할 일", "task": "리더십 연습 두페이지 읽기"}, ...]
        urgent_pool: 빠른 시일 내 처리할 일들 (LLM이 1-2개만 선별)
    """
    recurring: list[dict] = []
    urgent_pool: list[str] = []
    in_todo_root = False  # "# 할 일" 헤딩 이하인지
    current_h2 = ""
    matched = False
    in_urgent = False

    for b in blocks:
        btype = b.get("type")
        if btype == "heading_1":
            text = block_text(b)
            in_todo_root = "할 일" in text
            current_h2 = ""
            matched = False
            in_urgent = False
            continue
        if not in_todo_root:
            continue

        if btype == "heading_2":
            current_h2 = block_text(b)
            matched = heading_matches_today(current_h2, today)
            in_urgent = is_urgent_pool_heading(current_h2)
        elif btype == "to_do":
            text = block_text(b)
            if not text:
                continue
            if b["to_do"].get("checked", False):
                continue
            if matched:
                recurring.append({"section": current_h2, "task": text})
            elif in_urgent:
                urgent_pool.append(text)

    return recurring, urgent_pool


# ─────────────────────── iCloud Calendar ───────────────────────


def collect_calendar(today: datetime) -> list[dict]:
    """오늘 KST 00:00~24:00 일정을 iCloud CalDAV로 수집."""
    if not APPLE_ID or not APPLE_APP_PASSWORD:
        print("[Calendar] APPLE_ID/APPLE_APP_PASSWORD 미설정 — 건너뜀")
        return []

    try:
        client = caldav.DAVClient(
            url="https://caldav.icloud.com",
            username=APPLE_ID,
            password=APPLE_APP_PASSWORD,
        )
        calendars = client.principal().calendars()
    except Exception as e:
        print(f"[Calendar] iCloud 연결 실패: {e}")
        return []

    today_date = today.date()
    start = today.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    events: list[dict] = []
    dropped = 0

    for cal in calendars:
        cal_name = cal.get_display_name()
        try:
            found = cal.search(start=start, end=end, event=True, expand=True)
        except Exception as e:
            print(f"[Calendar] '{cal_name}' 검색 실패: {e}")
            continue
        for ev in found:
            try:
                # caldav 2.0+에서 vobject 의존성이 제거됨 → icalendar API 사용.
                # (구 ev.vobject_instance는 vobject 미설치 시 조용히 None을 반환해
                #  모든 일정이 누락되던 버그가 있었음)
                comp = ev.icalendar_component
                summary = str(comp.get("summary", "제목 없음"))
                dtstart = comp.get("dtstart").dt
                dtend_prop = comp.get("dtend")
                dtend = dtend_prop.dt if dtend_prop is not None else dtstart

                # 종일 일정은 날짜만 있는 floating 값이라, caldav가 검색창을 UTC로
                # 변환하는 과정에서 인접한 날(주로 어제) 일정이 오늘 창에 섞여 들어온다.
                # 실제 일정 날짜가 KST 오늘에 걸치는지 명시적으로 한 번 더 거른다.
                if not _event_on_kst_day(dtstart, dtend, today_date):
                    dropped += 1
                    continue

                if hasattr(dtstart, "hour"):
                    time_str = dtstart.astimezone(KST).strftime("%H:%M")
                    label = f"[{time_str}] {summary}"
                else:
                    label = f"[종일] {summary}"
                events.append({"label": label, "summary": summary})
            except Exception as e:
                print(f"[Calendar] '{cal_name}' 일정 파싱 실패: {type(e).__name__}: {e}")
                continue

    msg = f"[Calendar] {len(events)}개 일정 수집"
    if dropped:
        msg += f" (KST 오늘 아닌 {dropped}개 제외)"
    print(msg)
    return events


def _event_on_kst_day(dtstart, dtend, day: "datetime.date") -> bool:
    """일정이 KST 기준 `day`에 걸치는지 판정.

    - 시간 지정 일정(datetime): KST로 변환한 시작~종료 날짜 구간이 day를 포함
    - 종일 일정(date): iCal에서 dtend는 배타적(다음날) → [dtstart, dtend) 반열림 구간
    """
    if hasattr(dtstart, "hour"):  # 시간 지정
        start_d = dtstart.astimezone(KST).date()
        end_d = dtend.astimezone(KST).date() if hasattr(dtend, "hour") else start_d
        return start_d <= day <= end_d
    # 종일: dtend가 dtstart와 같거나 없으면 단일 종일로 간주
    end_date = dtend if (dtend and dtend > dtstart) else dtstart + timedelta(days=1)
    return dtstart <= day < end_date


# 일정 주체가 사용자 본인이 아니면(아래 인물의 일정) 기록하지 않는다.
# 키워드 매칭이 아니라 Claude가 맥락으로 판단 — "경민 슈주 모임"처럼 경민이 주체인
# 약속/레슨/모임/수업/인터뷰 등을 본인 할 일에서 걸러낸다.
EXCLUDE_PERSON = "경민"


def filter_others_calendar(events: list[dict], today: datetime) -> list[dict]:
    """캘린더 일정 중 '경민'의 일정(본인 할 일 아님)을 Claude가 판단해 제외."""
    if not events:
        return events
    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[Calendar-Filter] ANTHROPIC_API_KEY 미설정 — 필터 없이 전체 유지")
        return events

    weekday = WEEKDAY_KO[today.weekday()]
    listing = "\n".join(f"{i}: {e['label']}" for i, e in enumerate(events))
    prompt = f"""오늘은 {today.strftime("%Y-%m-%d")} {weekday}입니다.
아래는 사용자 캘린더에서 가져온 오늘 일정 목록입니다 (index: 일정).

{listing}

이 중 '{EXCLUDE_PERSON}'의 일정 — 즉 {EXCLUDE_PERSON}이(가) 주체이거나 {EXCLUDE_PERSON}을(를) 위한
약속·레슨·모임·수업·인터뷰·픽업 등, 사용자 본인이 직접 할 일이 아닌 일정 — 은 기록에서 제외합니다.
단, {EXCLUDE_PERSON}과 사용자가 함께 참석하는 일정(두 사람이 같이 언급되는 등)은 keep입니다.
사용자 본인이 직접 하거나 챙겨야 하는 일정의 index만 keep 배열로 돌려주세요.
판단이 애매하면 keep(남김)으로 둡니다.
"""
    schema = {
        "type": "object",
        "properties": {"keep": {"type": "array", "items": {"type": "integer"}}},
        "required": ["keep"],
        "additionalProperties": False,
    }
    try:
        text, _ = llm.generate(prompt, model=CLASSIFY_MODEL, max_tokens=512, schema=schema)
        keep = {i for i in json.loads(text)["keep"] if isinstance(i, int)}
    except Exception as e:
        print(f"[Calendar-Filter] 판단 실패({e}) — 필터 없이 전체 유지")
        return events

    kept = [e for i, e in enumerate(events) if i in keep]
    dropped = [e["label"] for i, e in enumerate(events) if i not in keep]
    if dropped:
        print(f"[Calendar-Filter] {EXCLUDE_PERSON} 일정 {len(dropped)}개 제외: {dropped}")
    return kept


# ─────────────────────── Claude 분류기 ───────────────────────


CLASSIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "assignments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "category": {"type": "string"},
                    "items": {"type": "array", "items": {"type": "integer"}},
                },
                "required": ["category", "items"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["assignments"],
    "additionalProperties": False,
}


def classify_items(
    categories: list[str],
    recurring: list[dict],
    calendar_events: list[dict],
    urgent_pool: list[str],
    today: datetime,
) -> dict[str, list[str]]:
    """반복 할 일 + 캘린더 + 빠른시일 추천을 카테고리별로 분류.

    항목을 번호로 제시하고 모델은 번호만 고르게 한다 — 모델이 텍스트를 echo하는 구조는
    시간 표기 변형("[11:00]"→"[10:00]")·누락으로 같은 일정이 중복 등록되는 사고가
    있었음 (2026-07-04). 번호 방식은 변형이 불가능하고, 누락은 코드에서 결정적으로 폴백.

    Returns: {"일": [...], "생활": [...], "공부": [...]}
    """
    if not (recurring or calendar_events or urgent_pool):
        return {c: [] for c in categories}

    # (텍스트, 종류, 필수 여부) — 필수 항목은 모델이 빠뜨리면 첫 카테고리로 폴백
    entries = (
        [(it["task"], "반복", True) for it in recurring]
        + [(e["label"], "캘린더", True) for e in calendar_events]
        + [(t, "빠른시일", False) for t in urgent_pool]
    )

    if not os.environ.get("ANTHROPIC_API_KEY") and not os.environ.get("GEMINI_API_KEY"):
        print("[Classifier] API 키 미설정 — 분류 없이 첫 카테고리에 모두 추가")
        fallback = [text for text, _kind, must in entries if must]
        return {c: (fallback if c == categories[0] else []) for c in categories}

    weekday = WEEKDAY_KO[today.weekday()]
    cat_list = ", ".join(categories)
    listing = "\n".join(f"{i}: ({kind}) {text}" for i, (text, kind, _must) in enumerate(entries))

    prompt = f"""오늘은 {today.strftime("%Y-%m-%d")} {weekday}입니다.

아래 할 일 항목들을 [{cat_list}] 카테고리로 분류해주세요.
응답의 items에는 항목 번호(정수)만 넣습니다.

{listing}

규칙:
- (반복)과 (캘린더) 항목은 빠짐없이 정확히 한 카테고리에 배치 (드롭 금지)
- (빠른시일) 항목은 오늘의 요일/일정에 맞는 1-2개만 골라 배치 (없으면 0개)
- 모든 카테고리를 응답에 포함하되, 항목이 없으면 빈 배열로

분류 가이드 (카테고리가 일/생활/공부일 때):
- 일: 회사 일, 회의, 회사 스터디, 발표 준비 등
- 생활: 자기관리, 회고/일기/원고/브이로그 등 창작·기록, 집안일, 취미, 운동, 약속, 가족 관련
- 공부: 책 읽기, 어학(링글 등), 자격증 준비(데이터브릭스 등), 알고리즘, 기술 학습
"""

    text, _ = llm.generate(prompt, model=CLASSIFY_MODEL, max_tokens=1024, schema=CLASSIFY_SCHEMA)
    data = json.loads(text)

    result = {c: [] for c in categories}
    placed: set[int] = set()
    for entry in data["assignments"]:
        cat = entry["category"] if entry["category"] in result else categories[0]
        for i in entry["items"]:
            if isinstance(i, int) and 0 <= i < len(entries) and i not in placed:
                placed.add(i)
                result[cat].append(entries[i][0])

    # 필수(반복/캘린더) 항목이 빠졌으면 첫 카테고리로 폴백 (절대 사라지지 않도록)
    missing = [i for i, (_t, _k, must) in enumerate(entries) if must and i not in placed]
    if missing:
        print(f"[Classifier] 모델이 누락한 필수 {len(missing)}개를 '{categories[0]}'로 폴백")
        result[categories[0]].extend(entries[i][0] for i in missing)

    print("[Classifier] " + ", ".join(f"{c}:{len(v)}" for c, v in result.items()))
    return result


# ─────────────────────── 페이지 빌드 ───────────────────────


def _todo_block_text(block: dict) -> str:
    """to_do 블록의 텍스트를 추출한다 (plain_text 우선, 없으면 text.content)."""
    parts = block.get("to_do", {}).get("rich_text", [])
    return "".join(
        rt.get("plain_text") or rt.get("text", {}).get("content", "") for rt in parts
    ).strip()


def build_page(
    today: str,
    parent_page_id: str,
    template_children: list[dict],
    classified: dict[str, list[str]],
) -> dict:
    """템플릿 + LLM 분류(오늘 캘린더/반복) 결과를 합쳐서 페이지 children 구성.

    매일 페이지는 "오늘의 할일"만 담는다 — 템플릿 고정 task + 오늘 KST 캘린더 일정 +
    오늘 날짜/요일에 해당하는 반복 할 일. 전날 미완료 항목 이월(carryover)은 하지
    않는다 (지난 일정/다른 요일 반복이 며칠씩 따라오는 누수 방지).
    """
    children = []
    current_category = ""
    seen: set[str] = set()  # 이미 추가한 task 텍스트 (중복 방지)

    def add_task(text: str) -> None:
        key = text.strip()
        if not key or key in seen:
            return
        seen.add(key)
        children.append(to_do_block(key))

    for block in template_children:
        # 템플릿 고정 task도 중복 추적 대상에 포함
        if block.get("type") == "to_do":
            key = _todo_block_text(block)
            if not key or key in seen:
                continue
            seen.add(key)
            children.append(block)
            continue

        children.append(block)
        if block.get("type") == "heading_2":
            current_category = "".join(
                t.get("plain_text", "") for t in block["heading_2"]["rich_text"]
            )
            for task in classified.get(current_category, []):
                add_task(task)

    return {
        "parent": {"type": "page_id", "page_id": parent_page_id},
        "icon": {"type": "emoji", "emoji": "✅"},
        "properties": {"title": [{"text": {"content": f"{today} TODO"}}]},
        "children": children,
    }


# ─────────────────── 연/월 sub-page 확보 ───────────────────


MONTH_PAGE_PATTERN = re.compile(r"^\d{4}-\d{2}$")


def ensure_year_page(year: str, root_id: str) -> str:
    """ROOT 안에서 '{year} 할일' 페이지를 찾고, 없으면 생성하여 page_id 반환.

    매년 1/1 첫 실행에서 자동으로 새 연도 페이지를 만든다. 'yearly 반복 패턴
    (# 할 일 섹션)'은 자동 복사하지 않으므로 사용자가 새 페이지에 직접 채워야
    매일 페이지에 반복 task가 들어간다.
    """
    title = f"{year} 할일"
    for b in get_blocks(root_id):
        if b.get("type") != "child_page":
            continue
        if b["child_page"].get("title", "") == title:
            return b["id"]

    print(f"[Year] '{title}' 페이지 신규 생성")
    result = api_request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": root_id},
        "icon": {"type": "emoji", "emoji": "🗓️"},
        "properties": {"title": [{"text": {"content": title}}]},
    })
    return result["id"]


def ensure_month_page(year_month: str, parent_id: str, existing: dict[str, str]) -> str:
    """parent_id 안에서 '{YYYY-MM}' sub-page를 찾고, 없으면 생성하여 page_id 반환."""
    if year_month in existing:
        return existing[year_month]

    print(f"[Month] '{year_month}' sub-page 신규 생성")
    result = api_request("POST", "/pages", {
        "parent": {"type": "page_id", "page_id": parent_id},
        "icon": {"type": "emoji", "emoji": "🗂️"},
        "properties": {"title": [{"text": {"content": year_month}}]},
    })
    page_id = result["id"]
    existing[year_month] = page_id
    return page_id


def collect_existing_month_pages(parent_id: str) -> dict[str, str]:
    """parent_id 직속의 'YYYY-MM' 월 sub-page id를 수집."""
    archives: dict[str, str] = {}
    for b in get_blocks(parent_id):
        if b.get("type") != "child_page":
            continue
        title = b["child_page"].get("title", "")
        if MONTH_PAGE_PATTERN.match(title):
            archives[title] = b["id"]
    return archives


# ─────────────────────── main ───────────────────────


def main(dry_run: bool = False):
    now = datetime.now(KST)
    today_str = now.strftime("%Y-%m-%d")
    yesterday_str = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    current_month = now.strftime("%Y-%m")
    current_year = now.strftime("%Y")

    print(f"{current_year} 할일 페이지 확보 중...")
    year_page_id = ensure_year_page(current_year, ROOT_PAGE_ID)

    print(f"월별 sub-page 확보 중... (현재 {current_month})")
    existing_archives = collect_existing_month_pages(year_page_id)
    month_page_id = ensure_month_page(current_month, year_page_id, existing_archives)

    print("템플릿 페이지 읽는 중...")
    template_blocks = get_blocks(TEMPLATE_PAGE_ID)
    template_children = blocks_to_children(template_blocks)
    categories = template_categories(template_children)
    print(f"  카테고리: {categories}")

    print(f"{current_year} 할일 페이지의 # 할 일 섹션 파싱 중...")
    recurring, urgent_pool = parse_recurring_todos(get_blocks(year_page_id), now)
    print(f"  반복 {len(recurring)}개, 빠른시일 풀 {len(urgent_pool)}개")

    deferred = collect_deferred_from_yesterday(yesterday_str)
    if deferred:
        print(f"  어제 '내일로' 미룬 항목 {len(deferred)}개 가져옴")
        recurring = recurring + [{"section": "내일로", "task": t} for t in deferred]

    calendar_events = filter_others_calendar(collect_calendar(now), now)

    classified = classify_items(categories, recurring, calendar_events, urgent_pool, now)

    body = build_page(today_str, month_page_id, template_children, classified)

    if dry_run:
        print(f"\n[dry-run] '{today_str} TODO' 페이지 생성/알림 생략. 구성 결과:")
        for block in body["children"]:
            btype = block["type"]
            if btype == "heading_2":
                print(f"\n[{''.join(t.get('plain_text') or t.get('text', {}).get('content', '') for t in block['heading_2']['rich_text'])}]")
            elif btype == "to_do":
                # Windows 콘솔(cp949)에서 인코딩 불가한 특수문자는 피한다
                print(f"  [ ] {_todo_block_text(block)}")
        return

    result = api_request("POST", "/pages", body)
    print(f"[{today_str} TODO] 생성 완료: {result.get('url', '')}")

    # 페이지 생성 직후 아침 알림까지 바로 발송 (구 morning-todo.yml 워크플로우 통합)
    # page_id를 직접 넘긴다 — search API는 방금 생성한 페이지를 못 찾음
    from src.morning_todo import run as send_morning_notification
    send_morning_notification(page_id=result["id"])


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
