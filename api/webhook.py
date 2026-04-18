"""Telegram 인라인 키보드 콜백을 처리하는 Vercel 서버리스 함수"""

import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.request import Request, urlopen


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
            if item_idx == index and "\u2713" not in line:
                lines[i] = line + " \u2713"
            item_idx += 1

    new_text = "\n".join(lines)

    # 남은 미완료 항목으로 키보드 재구성
    unchecked = []
    item_idx = 0
    for line in lines:
        stripped = line.strip()
        if stripped and stripped[0].isdigit() and ". " in stripped:
            if "\u2713" not in stripped:
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
            [{"text": f"{item['name']} \u2713", "callback_data": f"check:{item['idx']}"}]
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
