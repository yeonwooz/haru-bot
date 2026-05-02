"""Telegram 인라인 키보드 콜백 + 사용자 답장(코멘트/설정)을 처리하는 Vercel 서버리스 함수"""

import json
import os
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen

from notion_client import Client

KST = timezone(timedelta(hours=9))
SETTING_PREFIXES = ("/설정 ", "/set ")
SETTING_BARE = ("/설정", "/set")


def _telegram_api(method: str, data: dict) -> dict:
    """Telegram Bot API를 호출한다."""
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    url = f"https://api.telegram.org/bot{token}/{method}"
    req = Request(
        url,
        data=json.dumps(data).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urlopen(req, timeout=5) as resp:
        return json.loads(resp.read())


def _send_reply(chat_id: int, text: str):
    try:
        _telegram_api("sendMessage", {"chat_id": chat_id, "text": text})
    except Exception as e:
        print(f"[webhook] 답장 전송 실패: {e}")


def _notion_client_and_db():
    token = os.environ.get("NOTION_TOKEN")
    db_id = os.environ.get("NOTION_DIARY_DB_ID")
    if not token or not db_id:
        return None, None
    return Client(auth=token), db_id


def _find_diary_page(client: Client, db_id: str, date: str) -> str | None:
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


def _append_comment(client: Client, page_id: str, comment: str):
    page = client.pages.retrieve(page_id=page_id)
    rich_text = page["properties"].get("comment", {}).get("rich_text", [])
    existing = rich_text[0].get("text", {}).get("content", "") if rich_text else ""
    new_text = f"{existing}\n{comment}" if existing else comment
    client.pages.update(
        page_id=page_id,
        properties={"comment": {"rich_text": [{"text": {"content": new_text[:2000]}}]}},
    )


def _append_setting(client: Client, page_id: str, setting: str):
    page = client.pages.retrieve(page_id=page_id)
    rich_text = page["properties"].get("setting", {}).get("rich_text", [])
    existing = rich_text[0].get("text", {}).get("content", "") if rich_text else ""
    new_text = f"{existing}\n{setting}" if existing else setting
    client.pages.update(
        page_id=page_id,
        properties={"setting": {"rich_text": [{"text": {"content": new_text[:2000]}}]}},
    )


def _classify(text: str) -> tuple[str, str | None]:
    """답장을 (kind, payload)로 분류한다. kind ∈ {"comment", "setting", "ignore"}."""
    stripped = text.strip()
    for prefix in SETTING_PREFIXES:
        if stripped.startswith(prefix):
            payload = stripped[len(prefix):].strip()
            return ("setting", payload) if payload else ("ignore", None)
    if stripped in SETTING_BARE:
        return ("ignore", None)
    return ("comment", text)


def _handle_text_message(message: dict):
    """사용자 텍스트 답장을 처리해 오늘 일기에 반영한다."""
    chat_id = message["chat"]["id"]
    expected = os.environ.get("TELEGRAM_CHAT_ID")
    if expected and str(chat_id) != str(expected):
        return  # 다른 채팅은 무시

    text = message.get("text", "")
    if not text:
        return

    kind, payload = _classify(text)
    if kind == "ignore":
        return

    client, db_id = _notion_client_and_db()
    if not client:
        _send_reply(chat_id, "Notion 설정이 누락되어 저장하지 못했어요.")
        return

    today = datetime.now(KST).strftime("%Y-%m-%d")

    try:
        page_id = _find_diary_page(client, db_id, today)
        if not page_id:
            _send_reply(chat_id, f"{today} 일기를 아직 찾지 못했어요. 봇이 오늘 요약을 보낸 뒤에 답장해 주세요.")
            return

        if kind == "setting":
            _append_setting(client, page_id, payload)
            _send_reply(chat_id, f"설정 저장됨: {payload}")
        else:
            _append_comment(client, page_id, payload)
            _send_reply(chat_id, "코멘트 저장됨")
    except Exception as e:
        print(f"[webhook] Notion 반영 실패: {e}")
        _send_reply(chat_id, "저장 중 문제가 발생했어요.")


def _handle_check_callback(callback_query: dict):
    """체크리스트 버튼 콜백을 처리한다."""
    callback_id = callback_query["id"]
    data = callback_query.get("data", "")
    message = callback_query["message"]
    chat_id = message["chat"]["id"]
    message_id = message["message_id"]
    text = message["text"]

    # 로딩 스피너는 무슨 일이 있어도 먼저 끈다
    try:
        _telegram_api("answerCallbackQuery", {"callback_query_id": callback_id})
    except Exception:
        pass

    if not data.startswith("check:"):
        return

    try:
        index = int(data.split(":")[1])
    except ValueError:
        return

    # 메시지 텍스트에서 해당 항목에 ✓ 추가
    lines = text.split("\n")
    item_idx = 0
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and ". " in stripped:
            if item_idx == index and "✓" not in line:
                lines[i] = line + " ✓"
            item_idx += 1

    new_text = "\n".join(lines)

    # 남은 미완료 항목으로 키보드 재구성
    unchecked = []
    item_idx = 0
    for line in lines:
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and ". " in stripped:
            if "✓" not in stripped:
                name = stripped.split(". ", 1)[1]
                short = name[:30] + "..." if len(name) > 30 else name
                unchecked.append({"idx": item_idx, "name": short})
            item_idx += 1

    if not unchecked:
        new_text += "\n\n모두 완료! 오늘도 수고하셨습니다 :)"

    edit_data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
    }

    if unchecked:
        keyboard = [
            [{"text": f"{item['name']} ✓", "callback_data": f"check:{item['idx']}"}]
            for item in unchecked
        ]
        edit_data["reply_markup"] = {"inline_keyboard": keyboard}
    else:
        edit_data["reply_markup"] = {"inline_keyboard": []}

    try:
        _telegram_api("editMessageText", edit_data)
    except Exception:
        pass  # 이미 같은 내용이면 에러 — 무시


class handler(BaseHTTPRequestHandler):
    def do_POST(self):
        # 텔레그램 재시도를 막기 위해 어떤 경우에도 200을 반환한다.
        try:
            content_length = int(self.headers.get("Content-Length", 0))
            body = json.loads(self.rfile.read(content_length))
            if "callback_query" in body:
                _handle_check_callback(body["callback_query"])
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
