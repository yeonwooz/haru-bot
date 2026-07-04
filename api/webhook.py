"""Telegram 콜백/메시지를 처리하는 Vercel 서버리스 함수.

콜백:
- t:{pid}:{idx} — tasks 토글 + 키보드 갱신
- done:{pid} — 키보드 제거 + Claude 피드백 생성 + discussion 누적 + Telegram 전송
- s:{pid}:{idx} — Status select 저장 + discussion 누적 + Claude reply + Telegram 전송
- check:{idx} — legacy, 무시

메시지:
- 텍스트 → discussion에 '나: ...' append → Claude reply 생성 → '클로드: ...' append + 전송
- /설정 X / /set X → 별도 설정 페이지에 누적
- 사진(photo) → Telegram 다운로드 → Notion file_uploads 업로드 → 일기 페이지에 image block append
- 영상(video/animation/video document) → 노션 페이지 URL과 함께 직접 업로드 안내
- 봇 자기 메시지(from.is_bot)는 무시

Notion 데이터 모델은 src/diary_store.py docstring 참조 (discussion 단일 컬럼).
"""

import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import anthropic
from notion_client import Client

KST = timezone(timedelta(hours=9))
SETTING_PREFIXES = ("/설정 ", "/set ")
# "/일기 내용" 형태만 일기에 저장 (없는 날짜면 어느 일기인지 질문).
# prefix 없는 일반 텍스트는 저장하지 않고 클로드와 대화만 한다 (2026-07-04 변경).
DIARY_PREFIXES = ("/일기 ",)

# "/"로 시작하는데 위 명령이 아니거나 내용 없이 명령만 보낸 경우 안내
HELP_TEXT = (
    "사용법:\n"
    "/일기 <내용> : 일기에 저장 (오늘 일기가 없으면 어느 날짜인지 물어봐요)\n"
    "/설정 <규칙> : 하루 요약에 반영할 규칙 저장 (/set 도 가능)\n"
    "그냥 텍스트 : 저장하지 않고 클로드와 대화\n"
    "사진 : 오늘 일기에 첨부"
)

CLAUDE_MODEL = "claude-opus-4-6"
FEEDBACK_MAX_TOKENS = 16000
REPLY_MAX_TOKENS = 16000
DATE_PARSE_MAX_TOKENS = 16000

# 오늘 일기가 없을 때 사용자에게 "어느 날짜에 추가할지" 물어보는 prompt에 박는 마커.
# 사용자가 force_reply로 답장하면 reply_to_message.text를 검사해 우리가 보낸 prompt인지 식별하고,
# MSG_DELIMITER 뒤쪽을 원본 사용자 메시지로 복원한다 — pending state를 외부 저장소 없이 유지.
PENDING_DATE_MARKER = "[haru-bot:pending-date]"
MSG_DELIMITER = "\n──[메시지]──\n"

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"

# 사용자 설정(/설정 명령) 누적용 노션 페이지 — diary_store.NOTION_SETTINGS_PAGE_ID와 동일해야 함
NOTION_SETTINGS_PAGE_ID = "357bb67c-a90c-8166-bccd-d83857fa0e19"

# 노션 Status 컬럼(select 타입)의 옵션. 인덱스가 콜백 데이터에 사용되므로 순서 변경 주의.
STATUS_OPTIONS = ["좋아!", "별로", "낫 배드?"]

FEEDBACK_SYSTEM_PROMPT = """당신은 사용자의 하루를 함께 돌아보는 긍정적이고 합리적인 일기 도우미입니다.
사용자가 오늘 완료한 태스크를 보고, 3~5줄로 짧게 의견을 줍니다.

규칙:
- 친근하고 자연스러운 반말 톤
- 완료한 것을 구체적으로 짚어 칭찬
- 못한 태스크는 언급하지 않기 (입력으로 주어지지 않음)
- 완료한 게 0개여도 격려 톤 유지 (오늘 하루 자체는 의미 있다는 뉘앙스)
- 이모지·헤더 사용 금지, 일반 문장으로만"""

# 같은 컨테이너 안에서 Telegram이 재시도한 같은 update_id를 차단하기 위한 in-memory FIFO.
# Vercel 콜드 스타트로 다른 컨테이너에 retry가 가면 막을 수 없으나, 응답을 즉시 200으로
# 돌려주는 것과 함께 쓰면 retry 자체가 거의 발생하지 않는다.
_PROCESSED_UPDATE_IDS: list[int] = []
_DEDUPE_LIMIT = 200


def _is_duplicate_update(update_id: int) -> bool:
    if update_id in _PROCESSED_UPDATE_IDS:
        return True
    _PROCESSED_UPDATE_IDS.append(update_id)
    overflow = len(_PROCESSED_UPDATE_IDS) - _DEDUPE_LIMIT
    if overflow > 0:
        del _PROCESSED_UPDATE_IDS[:overflow]
    return False


REPLY_SYSTEM_PROMPT = """당신은 사용자의 하루를 함께 돌아보는 긍정적이고 합리적인 일기 도우미입니다.
지금까지의 대화 흐름을 보고 사용자의 마지막 메시지에 짧게(2~3줄) 반응합니다.

규칙:
- 친근하고 자연스러운 반말 톤
- 사실을 짚거나 가볍게 되묻거나 한 마디 거들기
- 너무 길게 늘어놓지 않기
- 이모지·헤더 사용 금지, 일반 문장으로만"""


def _env(key: str) -> str | None:
    """환경변수를 읽고 양 끝 공백·줄바꿈을 제거한다."""
    val = os.environ.get(key)
    return val.strip() if val else val


# --- Telegram ---

def _telegram_api(method: str, data: dict) -> dict:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN 미설정")
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urlopen(req, timeout=10) as resp:
            return json.loads(resp.read())
    except HTTPError as e:
        raise RuntimeError(f"telegram api {method} HTTP {e.code} {e.reason}") from None
    except Exception as e:
        raise RuntimeError(f"telegram api {method} failed: {type(e).__name__}") from None


def _send_message(chat_id: int, text: str):
    try:
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"[webhook] sendMessage 실패: {e}")


def _telegram_get_file_path(file_id: str) -> str | None:
    try:
        resp = _telegram_api("getFile", {"file_id": file_id})
    except Exception as e:
        print(f"[webhook] getFile 실패: {e}")
        return None
    if not resp.get("ok"):
        return None
    return resp.get("result", {}).get("file_path")


def _telegram_download_file(file_path: str) -> bytes | None:
    token = _env("TELEGRAM_BOT_TOKEN")
    if not token:
        return None
    url = f"https://api.telegram.org/file/bot{token}/{file_path}"
    try:
        with urlopen(url, timeout=20) as resp:
            return resp.read()
    except Exception as e:
        print(f"[webhook] file 다운로드 실패: {type(e).__name__}: {e}")
        return None


# --- Notion ---

def _notion_client_and_db():
    token = _env("NOTION_TOKEN")
    db_id = _env("NOTION_DIARY_DB_ID")
    if not token or not db_id:
        return None, None
    return Client(auth=token), db_id


def _read_tasks(client: Client, page_id: str):
    page = client.pages.retrieve(page_id=page_id)
    rich = page["properties"].get("tasks", {}).get("rich_text", [])
    if not rich:
        return []
    text = rich[0].get("text", {}).get("content", "")
    items = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.startswith("[x] ") or line.startswith("[X] "):
            items.append((line[4:], True))
        elif line.startswith("[ ] "):
            items.append((line[4:], False))
    return items


def _write_tasks(client: Client, page_id: str, items: list[tuple[str, bool]]):
    new_text = "\n".join(f"[{'x' if d else ' '}] {t}" for t, d in items)
    client.pages.update(
        page_id=page_id,
        properties={"tasks": {"rich_text": [{"text": {"content": new_text[:2000]}}]}},
    )


def _find_today_page_id(client: Client, db_id: str, date: str) -> str | None:
    db_id_clean = db_id.replace("-", "")
    results = client.search(filter={"property": "object", "value": "page"})
    for page in results.get("results", []):
        parent = page.get("parent", {})
        if parent.get("database_id", "").replace("-", "") != db_id_clean:
            continue
        date_prop = page["properties"].get("date", {}).get("date")
        if date_prop and date_prop.get("start") == date:
            return page["id"]
    return None


def _read_discussion(client: Client, page_id: str) -> str:
    page = client.pages.retrieve(page_id=page_id)
    rich = page["properties"].get("discussion", {}).get("rich_text", [])
    if not rich:
        return ""
    return rich[0].get("text", {}).get("content", "")


def _append_discussion(client: Client, page_id: str, role: str, text: str):
    """discussion 컬럼에 '{role}: {text}' 한 줄을 append한다 (2000자 컷)."""
    existing = _read_discussion(client, page_id)
    line = f"• {role}: {text}"
    new_text = f"{existing}\n{line}" if existing else line
    client.pages.update(
        page_id=page_id,
        properties={"discussion": {"rich_text": [{"text": {"content": new_text[:2000]}}]}},
    )


def _save_status(client: Client, page_id: str, status_name: str):
    client.pages.update(
        page_id=page_id,
        properties={"Status": {"select": {"name": status_name}}},
    )


def _get_page_url(client: Client, page_id: str) -> str | None:
    try:
        page = client.pages.retrieve(page_id=page_id)
        return page.get("url")
    except Exception:
        return None


def _get_page_date(client: Client, page_id: str) -> str | None:
    """일기 페이지의 date 컬럼(YYYY-MM-DD)을 반환. 없으면 None."""
    try:
        page = client.pages.retrieve(page_id=page_id)
        return page["properties"].get("date", {}).get("date", {}).get("start")
    except Exception as e:
        print(f"[webhook] date 조회 실패: {e}")
        return None


def _find_daily_todo_page_id(client: Client, date: str) -> str | None:
    """'{date} TODO' 제목의 페이지(notion-daily-todo가 만든 페이지) ID. 없으면 None."""
    title_query = f"{date} TODO"
    try:
        results = client.search(
            query=title_query,
            filter={"property": "object", "value": "page"},
        )
    except Exception as e:
        print(f"[webhook] daily-todo 검색 실패: {e}")
        return None
    for r in results.get("results", []):
        for prop in r.get("properties", {}).values():
            if prop.get("type") != "title":
                continue
            title_parts = prop.get("title", [])
            actual = "".join(t.get("plain_text", "") for t in title_parts).strip()
            if actual == title_query:
                return r["id"]
    return None


def _sync_todos_in_daily_page(client: Client, date: str, items: list[tuple[str, bool]]):
    """'{date} TODO' 페이지의 to_do 블록들을 items의 checked 상태에 일괄 맞춘다.

    🏁 완료 시점에 한 번 호출 — 각 토글마다 호출하면 API 비용이 늘고 응답이 느려지기 때문.
    매칭 실패(이름 불일치 / 페이지 없음 / nested to_do)는 silent.
    """
    daily_page_id = _find_daily_todo_page_id(client, date)
    if not daily_page_id:
        return

    desired: dict[str, bool] = {}
    for text, checked in items:
        desired[text.strip()] = checked

    cursor = None
    while True:
        try:
            resp = client.blocks.children.list(
                block_id=daily_page_id, page_size=100, start_cursor=cursor,
            )
        except Exception as e:
            print(f"[webhook] daily-todo blocks list 실패: {e}")
            return
        for b in resp.get("results", []):
            if b.get("type") != "to_do":
                continue
            text = "".join(rt.get("plain_text", "") for rt in b["to_do"].get("rich_text", [])).strip()
            if text not in desired:
                continue
            want = desired[text]
            if b["to_do"].get("checked", False) == want:
                continue
            try:
                client.blocks.update(block_id=b["id"], to_do={"checked": want})
                print(f"[webhook] daily-todo '{text}' → checked={want}")
            except Exception as e:
                print(f"[webhook] to_do 블록 갱신 실패: {e}")
        if not resp.get("has_more"):
            break
        cursor = resp.get("next_cursor")


def _append_deferred_to_daily(client: Client, date: str, items: list[str]):
    """사용자가 '내일로' 고른 항목을 '{date} TODO' 페이지의 '🔜 내일로' 섹션에 추가한다.

    다음날 06시 notion-daily-todo가 전날 페이지의 이 섹션만 읽어 오늘로 가져온다
    (선택적 이월). 매칭 키는 heading 텍스트의 '내일로' 부분.
    """
    daily_page_id = _find_daily_todo_page_id(client, date)
    if not daily_page_id:
        print("[webhook] daily-todo 페이지 없음 — 내일로 저장 건너뜀")
        return
    children = [
        {"object": "block", "type": "divider", "divider": {}},
        {"object": "block", "type": "heading_2",
         "heading_2": {"rich_text": [{"text": {"content": "🔜 내일로"}}]}},
    ]
    for t in items:
        children.append({
            "object": "block", "type": "to_do",
            "to_do": {"rich_text": [{"text": {"content": t[:2000]}}], "checked": False},
        })
    try:
        client.blocks.children.append(block_id=daily_page_id, children=children)
        print(f"[webhook] 내일로 {len(items)}개 저장 ('{date} TODO')")
    except Exception as e:
        print(f"[webhook] 내일로 저장 실패: {e}")


def _append_setting_to_settings_page(client: Client, setting_text: str):
    client.blocks.children.append(
        block_id=NOTION_SETTINGS_PAGE_ID,
        children=[{
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"type": "text", "text": {"content": setting_text[:2000]}}],
            },
        }],
    )


# --- Notion file upload (raw HTTP) ---

def _notion_upload_file(file_bytes: bytes, filename: str, mime: str) -> str | None:
    """Notion file_uploads API로 단일 파트 업로드. file_upload id를 반환한다.

    1) POST /v1/file_uploads — 빈 body로 upload object 생성 → upload_url 받음
    2) POST {upload_url} — multipart/form-data로 바이트 업로드
    """
    token = _env("NOTION_TOKEN")
    if not token:
        return None

    create_req = Request(
        f"{NOTION_API}/file_uploads",
        data=json.dumps({}).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlopen(create_req, timeout=15) as resp:
            upload = json.loads(resp.read())
    except Exception as e:
        print(f"[webhook] file_uploads 생성 실패: {type(e).__name__}: {e}")
        return None

    upload_id = upload.get("id")
    upload_url = upload.get("upload_url")
    if not upload_id or not upload_url:
        print(f"[webhook] file_uploads 응답 이상: {upload}")
        return None

    boundary = uuid.uuid4().hex
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8") + file_bytes + f"\r\n--{boundary}--\r\n".encode("utf-8")

    send_req = Request(
        upload_url,
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    try:
        with urlopen(send_req, timeout=30) as resp:
            resp.read()
    except Exception as e:
        print(f"[webhook] file_uploads 전송 실패: {type(e).__name__}: {e}")
        return None

    return upload_id


def _append_image_block(client: Client, page_id: str, file_upload_id: str):
    client.blocks.children.append(
        block_id=page_id,
        children=[{
            "object": "block",
            "type": "image",
            "image": {
                "type": "file_upload",
                "file_upload": {"id": file_upload_id},
            },
        }],
    )


# --- Claude ---

def _generate_feedback(completed: list[str]) -> str:
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    client = anthropic.Anthropic(api_key=api_key)
    done_text = "\n".join(f"- {t}" for t in completed) if completed else "(없음)"
    user_prompt = (
        f"오늘 완료한 태스크:\n{done_text}\n\n"
        "위를 보고 3~5줄로 의견을 줘."
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=FEEDBACK_MAX_TOKENS,
        system=[{"type": "text", "text": FEEDBACK_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


def _parse_date_answer(text: str, today: str) -> str | None:
    """사용자가 답한 짧은 한국어 텍스트를 YYYY-MM-DD로 변환. 해석 불가 시 None."""
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        return None
    client = anthropic.Anthropic(api_key=api_key)
    system = (
        f"오늘 날짜는 {today} (YYYY-MM-DD).\n"
        "사용자의 짧은 한국어 텍스트를 날짜로 변환해 JSON 한 줄로만 답하라.\n"
        '형식: {"date": "YYYY-MM-DD"} 또는 {"date": null}\n'
        "예: '어제' → 오늘 -1일, '그제'/'엊그제'/'그저께' → 오늘 -2일, "
        "'N일 전' → 오늘 -N일, 'M월 D일' → 가장 가까운 과거 날짜.\n"
        "날짜로 해석 못 하면 null."
    )
    try:
        msg = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=DATE_PARSE_MAX_TOKENS,
            system=[{"type": "text", "text": system}],
            messages=[{"role": "user", "content": text}],
        )
        raw = msg.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.strip("`")
            if raw.lower().startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        data = json.loads(raw)
        date = data.get("date")
        if not isinstance(date, str):
            return None
        datetime.strptime(date, "%Y-%m-%d")
        return date
    except Exception as e:
        print(f"[webhook] 날짜 파싱 실패: {e}")
        return None


def _generate_reply(discussion_text: str) -> str:
    """discussion 누적 텍스트를 보고 마지막 '나:' 메시지에 짧게 반응한다."""
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = (
        "지금까지의 대화 (각 줄은 '• 클로드: ...' 또는 '• 나: ...'):\n"
        f"{discussion_text}\n\n"
        "마지막 '• 나:' 메시지에 짧게(2~3줄) 반응해줘. 답에는 '클로드:' 같은 prefix나 '•' 같은 bullet 기호 붙이지 말고 본문만 적어."
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=REPLY_MAX_TOKENS,
        system=[{"type": "text", "text": REPLY_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


# --- Callback handlers ---

def _build_task_keyboard(items: list[tuple[str, bool]], pid_short: str):
    keyboard = []
    for i, (t, d) in enumerate(items):
        label = (t[:40] + "...") if len(t) > 40 else t
        mark = "☑" if d else "☐"
        keyboard.append([{"text": f"{mark} {label}", "callback_data": f"t:{pid_short}:{i}"}])
    keyboard.append([{"text": "🏁 완료", "callback_data": f"done:{pid_short}"}])
    return keyboard


def _handle_toggle(callback_query: dict, pid_short: str, index: int):
    callback_id = callback_query["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

    client, _ = _notion_client_and_db()
    if not client:
        return

    try:
        items = _read_tasks(client, pid_short)
        if index < 0 or index >= len(items):
            return
        text, done = items[index]
        items[index] = (text, not done)
        _write_tasks(client, pid_short, items)

        try:
            _telegram_api("editMessageReplyMarkup", {
                "chat_id": chat_id,
                "message_id": message_id,
                "reply_markup": {"inline_keyboard": _build_task_keyboard(items, pid_short)},
            })
        except Exception as e:
            print(f"[webhook] editMessageReplyMarkup 실패: {e}")
    except Exception as e:
        print(f"[webhook] 토글 처리 실패: {e}")


def _handle_done(callback_query: dict, pid_short: str):
    callback_id = callback_query["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    try:
        _telegram_api("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "피드백 생성 중...",
        })
    except Exception:
        pass

    try:
        _telegram_api("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception:
        pass

    client, _ = _notion_client_and_db()
    if not client:
        _send_message(chat_id, "Notion 설정이 없어 피드백 저장을 못했어요.")
        return

    try:
        items = _read_tasks(client, pid_short)

        # 노션 'YYYY-MM-DD TODO' 페이지의 to_do 블록 체크 상태를 일기 페이지 상태와 동기화
        date = _get_page_date(client, pid_short)
        if date:
            _sync_todos_in_daily_page(client, date, items)

        completed = [t for t, d in items if d]
        feedback = _generate_feedback(completed)
        _send_message(chat_id, feedback)
        try:
            _append_discussion(client, pid_short, "클로드", feedback)
        except Exception as e:
            print(f"[webhook] discussion 저장 실패: {e}")
    except Exception as e:
        print(f"[webhook] 피드백 처리 실패: {e}")
        _send_message(chat_id, "피드백 생성 중 문제가 발생했어요.")
        return

    # 완료 직후 "오늘 못한 것 중 내일로 미룰 항목" 인터랙션. 미완료가 없으면 건너뜀.
    uncompleted = [t for t, d in items if not d]
    if uncompleted:
        _send_defer_keyboard(chat_id, pid_short, uncompleted)
    else:
        _send_status_keyboard(chat_id, pid_short)


def _build_defer_keyboard(uncompleted: list[str], pid_short: str):
    keyboard = []
    for i, t in enumerate(uncompleted):
        label = (t[:40] + "...") if len(t) > 40 else t
        keyboard.append([{"text": f"☐ {label}", "callback_data": f"d:{pid_short}:{i}"}])
    keyboard.append([{"text": "🔜 내일로 넘기기", "callback_data": f"dd:{pid_short}"}])
    return keyboard


def _send_defer_keyboard(chat_id: int, pid_short: str, uncompleted: list[str]):
    try:
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": "오늘 못한 항목 중 내일로 미룰 것들을 체크해줘.",
            "reply_markup": {"inline_keyboard": _build_defer_keyboard(uncompleted, pid_short)},
        })
    except Exception as e:
        print(f"[webhook] defer 키보드 전송 실패: {e}")


def _handle_defer_toggle(callback_query: dict, pid_short: str, index: int):
    """내일로-미룰 후보 버튼의 ☐/☑ 표시를 토글한다 (선택 상태는 키보드 마크업에만 보관)."""
    callback_id = callback_query["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

    markup = message.get("reply_markup", {}).get("inline_keyboard", [])
    target = f"d:{pid_short}:{index}"
    for row in markup:
        for btn in row:
            if btn.get("callback_data") != target:
                continue
            txt = btn.get("text", "")
            if txt.startswith("☑"):
                btn["text"] = "☐" + txt[1:]
            elif txt.startswith("☐"):
                btn["text"] = "☑" + txt[1:]
    try:
        _telegram_api("editMessageReplyMarkup", {
            "chat_id": chat_id,
            "message_id": message_id,
            "reply_markup": {"inline_keyboard": markup},
        })
    except Exception as e:
        print(f"[webhook] defer 토글 갱신 실패: {e}")


def _handle_defer_done(callback_query: dict, pid_short: str):
    """☑로 고른 항목을 그날 TODO 페이지의 '🔜 내일로' 섹션에 저장하고 status로 넘어간다."""
    callback_id = callback_query["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

    # 마크업에서 ☑ 표시된 후보의 index 수집
    markup = message.get("reply_markup", {}).get("inline_keyboard", [])
    selected: list[int] = []
    for row in markup:
        for btn in row:
            cd = btn.get("callback_data", "")
            if cd.startswith(f"d:{pid_short}:") and btn.get("text", "").startswith("☑"):
                try:
                    selected.append(int(cd.split(":")[2]))
                except ValueError:
                    pass

    client, _ = _notion_client_and_db()
    deferred: list[str] = []
    if client and selected:
        try:
            items = _read_tasks(client, pid_short)
            uncompleted = [t for t, d in items if not d]
            deferred = [uncompleted[i] for i in selected if 0 <= i < len(uncompleted)]
            date = _get_page_date(client, pid_short)
            if deferred and date:
                _append_deferred_to_daily(client, date, deferred)
        except Exception as e:
            print(f"[webhook] 내일로 처리 실패: {e}")

    text = f"{len(deferred)}개를 내일로 넘겼어." if deferred else "내일로 넘긴 항목은 없어."
    try:
        _telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception as e:
        print(f"[webhook] defer 완료 메시지 갱신 실패: {e}")

    _send_status_keyboard(chat_id, pid_short)


def _send_status_keyboard(chat_id: int, pid_short: str):
    keyboard = [
        [{"text": opt, "callback_data": f"s:{pid_short}:{i}"}]
        for i, opt in enumerate(STATUS_OPTIONS)
    ]
    try:
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": "오늘 하루 어땠어?",
            "reply_markup": {"inline_keyboard": keyboard},
        })
    except Exception as e:
        print(f"[webhook] status 키보드 전송 실패: {e}")


def _handle_status(callback_query: dict, pid_short: str, index: int):
    callback_id = callback_query["id"]
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]

    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

    if index < 0 or index >= len(STATUS_OPTIONS):
        return
    name = STATUS_OPTIONS[index]

    client, _ = _notion_client_and_db()
    if not client:
        return

    try:
        _save_status(client, pid_short, name)
    except Exception as e:
        print(f"[webhook] status 저장 실패: {e}")
        return

    try:
        _telegram_api("editMessageText", {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": f'오늘 하루는 "{name}". 기록했어.',
            "reply_markup": {"inline_keyboard": []},
        })
    except Exception as e:
        print(f"[webhook] status 메시지 갱신 실패: {e}")

    # 기분 응답은 Status select 컬럼에만 저장하고 discussion/reply는 생략한다.
    # 단답("좋아!", "별로" 등)에 일일이 반응하면 어색하고 흐름이 무거워짐.


def _handle_legacy_check(callback_query: dict):
    callback_id = callback_query["id"]
    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass
    print("[webhook] legacy check: 콜백 - 무시")


# --- Message handlers ---

def _classify_text(text: str) -> tuple[str, str | None]:
    stripped = text.strip()
    for prefix in SETTING_PREFIXES:
        if stripped.startswith(prefix):
            payload = stripped[len(prefix):].strip()
            return ("setting", payload) if payload else ("help", None)
    for prefix in DIARY_PREFIXES:
        if stripped.startswith(prefix):
            payload = stripped[len(prefix):].strip()
            return ("diary", payload) if payload else ("help", None)
    # "/", "/일기"(내용 없음), 모르는 "/명령" 전부 → 사용법 안내
    if stripped.startswith("/"):
        return ("help", None)
    return ("chat", text)


def _send_pending_date_prompt(chat_id: int, today: str, user_text: str):
    """오늘 일기가 없을 때 어느 날짜에 추가할지 묻는 force_reply prompt. 사용자 메시지를
    prompt 본문에 박아 두면 답장 update의 reply_to_message로 그대로 복원 가능 — 외부 저장소 불필요."""
    prompt = (
        f"{PENDING_DATE_MARKER}\n"
        f"오늘({today}) 일기가 아직 없네. 어느 날짜 일기에 추가할까?\n"
        '예) "어제", "그제", "2026-05-14"\n\n'
        "답장으로 날짜만 알려줘. 메시지는 그대로 보관할게."
        f"{MSG_DELIMITER}{user_text}"
    )
    try:
        _telegram_api("sendMessage", {
            "chat_id": chat_id,
            "text": prompt,
            "reply_markup": {"force_reply": True},
        })
    except Exception as e:
        print(f"[webhook] pending-date prompt 전송 실패: {e}")


def _extract_pending_text(reply_to: dict | None) -> str | None:
    """reply_to_message가 우리 pending-date prompt인지 확인하고 원본 사용자 메시지를 반환.
    아니면 None — 우리 prompt가 아니거나 형식이 깨진 경우."""
    if not reply_to:
        return None
    if not reply_to.get("from", {}).get("is_bot"):
        return None
    text = reply_to.get("text", "")
    if not text.startswith(PENDING_DATE_MARKER):
        return None
    if MSG_DELIMITER not in text:
        return None
    return text.split(MSG_DELIMITER, 1)[1]


def _handle_date_answer(message: dict, original_text: str):
    """pending-date prompt에 대한 답장 처리: 날짜 파싱 → 해당 페이지에 메시지 누적."""
    chat_id = message["chat"]["id"]
    expected = _env("TELEGRAM_CHAT_ID")
    if expected and str(chat_id) != expected:
        return

    date_input = (message.get("text") or "").strip()
    if not date_input:
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")
    target_date = _parse_date_answer(date_input, today)
    if not target_date:
        _send_message(chat_id, '날짜를 못 알아들었어. 다시 알려줄래? (예: "어제", "2026-05-14")')
        return

    client, db_id = _notion_client_and_db()
    if not client:
        _send_message(chat_id, "Notion 설정이 누락돼서 처리하지 못했어.")
        return

    try:
        page_id = _find_today_page_id(client, db_id, target_date)
    except Exception as e:
        print(f"[webhook] {target_date} 페이지 조회 실패: {e}")
        _send_message(chat_id, "일기 페이지 찾는 중 문제가 생겼어.")
        return

    if not page_id:
        _send_message(chat_id, f"{target_date} 일기가 없어. 기존 일기가 있는 날짜로 다시 답장해줘.")
        return

    _send_message(chat_id, f"{target_date} 일기에 추가할게.")
    _process_user_input(client, page_id, chat_id, original_text)


def _process_user_input(client: Client, page_id: str, chat_id: int, text: str):
    """discussion에 '나:' append → reply 생성 → '클로드:' append + 전송."""
    try:
        _append_discussion(client, page_id, "나", text)
    except Exception as e:
        print(f"[webhook] '나' append 실패: {e}")
        return
    try:
        discussion = _read_discussion(client, page_id)
        reply = _generate_reply(discussion)
        _send_message(chat_id, reply)
        _append_discussion(client, page_id, "클로드", reply)
    except Exception as e:
        print(f"[webhook] reply 생성 실패: {e}")


def _check_chat_and_resolve_today(message: dict) -> tuple[Client | None, str | None, int | None]:
    """공통 게이트: 봇 자기 메시지 차단, chat 화이트리스트, 오늘 일기 page_id 조회.

    반환: (client, page_id, chat_id) — 진행 가능하면 셋 다 채워짐, 아니면 어딘가 None.
    """
    if message.get("from", {}).get("is_bot"):
        return None, None, None
    chat_id = message["chat"]["id"]
    expected = _env("TELEGRAM_CHAT_ID")
    if expected and str(chat_id) != expected:
        return None, None, chat_id

    client, db_id = _notion_client_and_db()
    if not client:
        _send_message(chat_id, "Notion 설정이 누락되어 처리하지 못했어요.")
        return None, None, chat_id

    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        page_id = _find_today_page_id(client, db_id, today)
    except Exception as e:
        print(f"[webhook] 오늘 일기 조회 실패: {e}")
        _send_message(chat_id, "오늘 일기를 찾는 중 문제가 발생했어요.")
        return None, None, chat_id

    if not page_id:
        _send_message(chat_id, f"{today} 일기를 아직 찾지 못했어요. 봇이 오늘 요약을 보낸 뒤에 보내주세요.")
        return None, None, chat_id

    return client, page_id, chat_id


def _handle_text_message(message: dict):
    if message.get("from", {}).get("is_bot"):
        return
    chat_id = message["chat"]["id"]
    expected = _env("TELEGRAM_CHAT_ID")
    if expected and str(chat_id) != expected:
        return

    # pending-date prompt에 대한 force_reply 답장이면 별도 흐름으로 라우팅
    pending_text = _extract_pending_text(message.get("reply_to_message"))
    if pending_text is not None:
        _handle_date_answer(message, pending_text)
        return

    text = message.get("text", "")
    if not text:
        return

    kind, payload = _classify_text(text)
    if kind == "ignore":
        return
    if kind == "help":
        _send_message(chat_id, HELP_TEXT)
        return

    client, db_id = _notion_client_and_db()
    if not client:
        _send_message(chat_id, "Notion 설정이 누락되어 저장하지 못했어요.")
        return

    # 설정은 일기 페이지 무관하게 별도 페이지에 누적
    if kind == "setting":
        try:
            _append_setting_to_settings_page(client, payload)
            _send_message(chat_id, f"설정 저장됨: {payload}")
        except Exception as e:
            print(f"[webhook] 설정 페이지 반영 실패: {e}")
            _send_message(chat_id, "설정 저장 중 문제가 발생했어요.")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")

    # prefix 없는 일반 텍스트: 일기 저장 없이 대화만 (오늘 일기가 있으면 읽기 전용 컨텍스트로 활용)
    if kind == "chat":
        context = ""
        try:
            page_id = _find_today_page_id(client, db_id, today)
            if page_id:
                context = _read_discussion(client, page_id)
        except Exception as e:
            print(f"[webhook] 대화 컨텍스트 조회 실패(무시): {e}")
        try:
            reply = _generate_reply(f"{context}\n• 나: {payload}".strip())
            _send_message(chat_id, reply)
        except Exception as e:
            print(f"[webhook] 대화 응답 생성 실패: {e}")
            _send_message(chat_id, "응답 생성 중 문제가 생겼어.")
        return

    # kind == "diary": /일기 prefix — 오늘 일기에 저장, 오늘 일기가 없으면 날짜 질문
    try:
        page_id = _find_today_page_id(client, db_id, today)
        if not page_id:
            _send_pending_date_prompt(chat_id, today, payload)
            return
        _process_user_input(client, page_id, chat_id, payload)
    except Exception as e:
        print(f"[webhook] 코멘트 처리 실패: {e}")
        _send_message(chat_id, "저장 중 문제가 발생했어요.")


def _handle_photo_message(message: dict):
    client, page_id, chat_id = _check_chat_and_resolve_today(message)
    if not client or not page_id or chat_id is None:
        return

    photos = message.get("photo", [])
    if not photos:
        return
    largest = max(photos, key=lambda p: p.get("file_size", 0))
    file_id = largest.get("file_id")
    if not file_id:
        return

    file_path = _telegram_get_file_path(file_id)
    if not file_path:
        _send_message(chat_id, "사진 정보를 가져오지 못했어요.")
        return

    file_bytes = _telegram_download_file(file_path)
    if not file_bytes:
        _send_message(chat_id, "사진을 다운로드하지 못했어요.")
        return

    filename = os.path.basename(file_path) or f"{file_id}.jpg"
    upload_id = _notion_upload_file(file_bytes, filename, "image/jpeg")
    if not upload_id:
        _send_message(chat_id, "노션 업로드에 실패했어요.")
        return

    try:
        _append_image_block(client, page_id, upload_id)
    except Exception as e:
        print(f"[webhook] image block append 실패: {e}")
        _send_message(chat_id, "노션에 사진을 첨부하지 못했어요.")
        return

    _send_message(chat_id, "사진을 일기에 첨부했어.")

    caption = message.get("caption")
    if caption:
        _process_user_input(client, page_id, chat_id, caption)


def _handle_video_message(message: dict):
    client, page_id, chat_id = _check_chat_and_resolve_today(message)
    if not client or not page_id or chat_id is None:
        return

    url = _get_page_url(client, page_id) or f"https://www.notion.so/{page_id.replace('-', '')}"
    _send_message(chat_id, f"영상은 직접 업로드해주세요:\n{url}")


# --- HTTP handler ---

class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 1) Telegram이 timeout으로 재시도하지 않도록 200을 먼저 돌려준다.
        #    body 처리는 그 뒤에 진행. Vercel Python 런타임이 응답을 client에
        #    먼저 흘려보낸 뒤 함수가 끝날 때까지 컨테이너를 살려두기를 기대.
        content_length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(content_length)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
            try:
                self.wfile.flush()
            except Exception:
                pass
        except Exception:
            pass

        try:
            body = json.loads(raw)
        except Exception as e:
            print(f"[webhook] body 파싱 실패: {e}")
            return

        # 2) 같은 update_id 중복 처리 차단
        update_id = body.get("update_id")
        if isinstance(update_id, int) and _is_duplicate_update(update_id):
            print(f"[webhook] 중복 update_id={update_id} 무시")
            return

        try:
            if "callback_query" in body:
                cq = body["callback_query"]
                data = cq.get("data", "")
                if data.startswith("t:"):
                    parts = data.split(":")
                    if len(parts) == 3:
                        try:
                            idx = int(parts[2])
                            _handle_toggle(cq, parts[1], idx)
                        except ValueError:
                            pass
                elif data.startswith("done:"):
                    parts = data.split(":")
                    if len(parts) == 2:
                        _handle_done(cq, parts[1])
                elif data.startswith("dd:"):
                    parts = data.split(":")
                    if len(parts) == 2:
                        _handle_defer_done(cq, parts[1])
                elif data.startswith("d:"):
                    parts = data.split(":")
                    if len(parts) == 3:
                        try:
                            idx = int(parts[2])
                            _handle_defer_toggle(cq, parts[1], idx)
                        except ValueError:
                            pass
                elif data.startswith("s:"):
                    parts = data.split(":")
                    if len(parts) == 3:
                        try:
                            idx = int(parts[2])
                            _handle_status(cq, parts[1], idx)
                        except ValueError:
                            pass
                elif data.startswith("check:"):
                    _handle_legacy_check(cq)
            elif "message" in body:
                msg = body["message"]
                if msg.get("photo"):
                    _handle_photo_message(msg)
                elif msg.get("video") or msg.get("animation"):
                    _handle_video_message(msg)
                elif msg.get("document"):
                    mime = msg.get("document", {}).get("mime_type", "")
                    if mime.startswith("video/"):
                        _handle_video_message(msg)
                elif msg.get("text"):
                    _handle_text_message(msg)
        except Exception as e:
            print(f"[webhook] 처리 오류: {e}")

    def log_message(self, format, *args):
        pass
