"""Telegram 콜백/메시지를 처리하는 Vercel 서버리스 함수.

콜백:
- t:{pid}:{idx} — tasks 토글 + 키보드 갱신
- done:{pid} — 키보드 제거 + Claude 피드백 생성 + 노션 feedback 저장 + Telegram 전송
- check:{idx} — legacy, 무시

메시지:
- 일반 텍스트 → 오늘 일기 comment append
- /설정 X / /set X → 오늘 일기 setting append
- 봇 자기 메시지(from.is_bot)는 무시
"""

import json
import os
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


def _env(key: str) -> str | None:
    """환경변수를 읽고 양 끝 공백·줄바꿈을 제거한다."""
    val = os.environ.get(key)
    return val.strip() if val else val


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
        # HTTP 응답 에러는 status/reason 노출 (텔레그램 응답 body에는 토큰이 안 들어감)
        # 토큰 잘못 → 401 Unauthorized, 페이로드 형식 잘못 → 400 Bad Request 식으로 즉시 파악 가능
        raise RuntimeError(f"telegram api {method} HTTP {e.code} {e.reason}") from None
    except Exception as e:
        # client-side 에러(URLError 등)는 메시지에 URL/토큰이 들어갈 수 있어 type 이름만 노출
        raise RuntimeError(f"telegram api {method} failed: {type(e).__name__}") from None


def _send_message(chat_id: int, text: str):
    try:
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"[webhook] sendMessage 실패: {e}")


def _notion_client_and_db():
    token = _env("NOTION_TOKEN")
    db_id = _env("NOTION_DIARY_DB_ID")
    if not token or not db_id:
        return None, None
    return Client(auth=token), db_id


# --- Notion helpers ---

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


def _append_text_prop(client: Client, page_id: str, prop: str, text: str):
    page = client.pages.retrieve(page_id=page_id)
    rich = page["properties"].get(prop, {}).get("rich_text", [])
    existing = rich[0].get("text", {}).get("content", "") if rich else ""
    new_text = f"{existing}\n{text}" if existing else text
    client.pages.update(
        page_id=page_id,
        properties={prop: {"rich_text": [{"text": {"content": new_text[:2000]}}]}},
    )


def _save_feedback(client: Client, page_id: str, feedback: str):
    client.pages.update(
        page_id=page_id,
        properties={"feedback": {"rich_text": [{"text": {"content": feedback[:2000]}}]}},
    )


def _save_status(client: Client, page_id: str, status_name: str):
    client.pages.update(
        page_id=page_id,
        properties={"Status": {"select": {"name": status_name}}},
    )


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


# --- Claude feedback ---

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
            _save_feedback(client, pid_short, feedback)
        except Exception as e:
            print(f"[webhook] feedback 저장 실패: {e}")
    except Exception as e:
        print(f"[webhook] 피드백 처리 실패: {e}")
        _send_message(chat_id, "피드백 생성 중 문제가 발생했어요.")
        return

    # 피드백 후 status 키보드 송출
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
        try:
            _telegram_api("editMessageText", {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": f'오늘 하루는 "{name}". 기록했어.',
                "reply_markup": {"inline_keyboard": []},
            })
        except Exception as e:
            print(f"[webhook] status 메시지 갱신 실패: {e}")
    except Exception as e:
        print(f"[webhook] status 저장 실패: {e}")


def _handle_legacy_check(callback_query: dict):
    callback_id = callback_query["id"]
    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass
    print("[webhook] legacy check: 콜백 - 무시")


# --- Text message handler ---

def _classify_text(text: str) -> tuple[str, str | None]:
    stripped = text.strip()
    for prefix in SETTING_PREFIXES:
        if stripped.startswith(prefix):
            payload = stripped[len(prefix):].strip()
            return ("setting", payload) if payload else ("ignore", None)
    if stripped in SETTING_BARE:
        return ("ignore", None)
    return ("comment", text)


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

    # 설정은 별도 설정 페이지 본문에 누적 (일기 페이지 무관)
    if kind == "setting":
        try:
            _append_setting_to_settings_page(client, payload)
            _send_message(chat_id, f"설정 저장됨: {payload}")
        except Exception as e:
            print(f"[webhook] 설정 페이지 반영 실패: {e}")
            _send_message(chat_id, "설정 저장 중 문제가 발생했어요.")
        return

    # 코멘트는 오늘 일기에 append
    today = datetime.now(KST).strftime("%Y-%m-%d")
    try:
        page_id = _find_today_page_id(client, db_id, today)
        if not page_id:
            _send_message(chat_id, f"{today} 일기를 아직 찾지 못했어요. 봇이 오늘 요약을 보낸 뒤에 답장해 주세요.")
            return
        _append_text_prop(client, page_id, "comment", payload)
        _send_message(chat_id, "코멘트 저장됨")
    except Exception as e:
        print(f"[webhook] Notion 반영 실패: {e}")
        _send_message(chat_id, "저장 중 문제가 발생했어요.")


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
            elif "message" in body and body["message"].get("text"):
                _handle_text_message(body["message"])
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
