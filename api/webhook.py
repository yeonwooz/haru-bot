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
SETTING_BARE = ("/설정", "/set")

CLAUDE_MODEL = "claude-opus-4-6"
FEEDBACK_MAX_TOKENS = 400
REPLY_MAX_TOKENS = 300

NOTION_VERSION = "2025-09-03"
NOTION_API = "https://api.notion.com/v1"

# 사용자 설정(/설정 명령) 누적용 노션 페이지 (노션 환경 specific)
NOTION_SETTINGS_PAGE_ID = "30fbb67c-a90c-8024-9ca3-ee6af0d9f223"

# 노션 Status 컬럼(select 타입)의 옵션. 인덱스가 콜백 데이터에 사용되므로 순서 변경 주의.
STATUS_OPTIONS = ["좋아!", "별로", "낫 배드?"]

FEEDBACK_SYSTEM_PROMPT = """당신은 사용자의 하루를 함께 돌아보는 따뜻한 일기 도우미입니다.
사용자가 오늘 완료한 태스크와 못한 태스크를 보고, 3~5줄로 짧게 의견을 줍니다.

규칙:
- 친근하고 자연스러운 반말 톤
- 완료한 것이 있으면 구체적으로 짚어 칭찬
- 못한 것에 대해서는 자책하지 않게 위로하거나 가볍게 넘김
- 완료한 게 0개여도 격려 톤 유지 (오늘 하루 자체는 의미 있다는 뉘앙스)
- 이모지·헤더 사용 금지, 일반 문장으로만"""

REPLY_SYSTEM_PROMPT = """당신은 사용자의 하루를 함께 돌아보는 따뜻한 일기 도우미입니다.
지금까지의 대화 흐름을 보고 사용자의 마지막 메시지에 짧게(2~3줄) 반응합니다.

규칙:
- 친근하고 자연스러운 반말 톤
- 공감하거나 가볍게 되묻거나 한 마디 거들기
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
    line = f"{role}: {text}"
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

def _generate_feedback(completed: list[str], uncompleted: list[str]) -> str:
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    client = anthropic.Anthropic(api_key=api_key)
    done_text = "\n".join(f"- {t}" for t in completed) if completed else "(없음)"
    undone_text = "\n".join(f"- {t}" for t in uncompleted) if uncompleted else "(없음)"
    user_prompt = (
        f"오늘 완료한 태스크:\n{done_text}\n\n"
        f"오늘 못한 태스크:\n{undone_text}\n\n"
        "위를 보고 3~5줄로 의견을 줘."
    )
    msg = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=FEEDBACK_MAX_TOKENS,
        system=[{"type": "text", "text": FEEDBACK_SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": user_prompt}],
    )
    return msg.content[0].text


def _generate_reply(discussion_text: str) -> str:
    """discussion 누적 텍스트를 보고 마지막 '나:' 메시지에 짧게 반응한다."""
    api_key = _env("ANTHROPIC_API_KEY")
    if not api_key:
        raise RuntimeError("ANTHROPIC_API_KEY 미설정")
    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = (
        "지금까지의 대화 (각 줄은 '클로드: ...' 또는 '나: ...'):\n"
        f"{discussion_text}\n\n"
        "마지막 '나:' 메시지에 짧게(2~3줄) 따뜻하게 반응해줘. 답에는 '클로드:' 같은 prefix 붙이지 말고 본문만 적어."
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
        completed = [t for t, d in items if d]
        uncompleted = [t for t, d in items if not d]
        feedback = _generate_feedback(completed, uncompleted)
        _send_message(chat_id, feedback)
        try:
            _append_discussion(client, pid_short, "클로드", feedback)
        except Exception as e:
            print(f"[webhook] discussion 저장 실패: {e}")
    except Exception as e:
        print(f"[webhook] 피드백 처리 실패: {e}")
        _send_message(chat_id, "피드백 생성 중 문제가 발생했어요.")
        return

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

    # discussion 누적 + 후속 reply
    try:
        _append_discussion(client, pid_short, "나", name)
        discussion = _read_discussion(client, pid_short)
        reply = _generate_reply(discussion)
        _send_message(chat_id, reply)
        _append_discussion(client, pid_short, "클로드", reply)
    except Exception as e:
        print(f"[webhook] status reply 처리 실패: {e}")


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
            return ("setting", payload) if payload else ("ignore", None)
    if stripped in SETTING_BARE:
        return ("ignore", None)
    return ("comment", text)


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

    text = message.get("text", "")
    if not text:
        return

    kind, payload = _classify_text(text)
    if kind == "ignore":
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
    try:
        page_id = _find_today_page_id(client, db_id, today)
        if not page_id:
            _send_message(chat_id, f"{today} 일기를 아직 찾지 못했어요. 봇이 오늘 요약을 보낸 뒤에 답장해 주세요.")
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
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
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

        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True}).encode())
        except Exception:
            pass

    def log_message(self, format, *args):
        pass
