"""Vercel Cron이 호출하는 GitHub Actions 트리거 함수.

GitHub의 schedule 트리거는 큐 지연이 1~10시간까지 벌어지고 실행이 누락되는 날도
있어서(2026-08 관측), 스케줄링만 Vercel Cron으로 옮기고 실제 작업은 그대로
Actions에서 돌린다. 이 함수는 workflow_dispatch만 쏘고 바로 끝난다.

Vercel Cron은 GET으로 호출하고, 어느 스케줄이 트리거했는지를
x-vercel-cron-schedule 헤더(cron 표현식 원문)로 알려준다. 엔드포인트가 하나라서
이 헤더로 워크플로를 구분한다 — SCHEDULE_TO_WORKFLOW의 키는 vercel.json의
crons[].schedule과 문자 단위로 같아야 한다.

필요한 환경변수 (Vercel 프로젝트 Settings → Environment Variables):
- CRON_SECRET       : Vercel이 Authorization: Bearer <값>으로 자동 전송. 이 값이
                      없거나 안 맞으면 401. (외부에서 URL만 알아도 못 쏘게 하는 용도)
- GH_DISPATCH_TOKEN : fine-grained PAT, haru-bot 레포의 Actions read/write만.
"""

import hmac
import json
import os
from http.server import BaseHTTPRequestHandler
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

GITHUB_REPO = "yeonwooz/haru-bot"
GITHUB_REF = "master"

# vercel.json의 crons[].schedule → 트리거할 워크플로 파일명 (UTC 기준)
SCHEDULE_TO_WORKFLOW = {
    "0 12 * * *": "daily.yml",  # 21:00 KST — 하루 정리
    "0 21 * * *": "daily-todo.yml",  # 06:00 KST (다음날) — 아침 TODO
}


def _dispatch(workflow: str) -> None:
    """workflow_dispatch 호출. 성공 시 204, 실패 시 예외."""
    token = os.environ.get("GH_DISPATCH_TOKEN")
    if not token:
        raise RuntimeError("GH_DISPATCH_TOKEN 미설정")

    req = Request(
        f"https://api.github.com/repos/{GITHUB_REPO}/actions/workflows/{workflow}/dispatches",
        data=json.dumps({"ref": GITHUB_REF}).encode(),
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "haru-bot-cron",
        },
        method="POST",
    )
    with urlopen(req, timeout=10) as res:
        print(f"[cron] {workflow} dispatch 완료 (HTTP {res.status})")


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        secret = os.environ.get("CRON_SECRET")
        auth = self.headers.get("Authorization", "")
        if not secret or not hmac.compare_digest(auth, f"Bearer {secret}"):
            print("[cron] 인증 실패 — CRON_SECRET 미설정이거나 헤더 불일치")
            self._reply(401, {"ok": False, "error": "unauthorized"})
            return

        schedule = self.headers.get("x-vercel-cron-schedule", "")
        workflow = SCHEDULE_TO_WORKFLOW.get(schedule)
        if not workflow:
            # vercel.json의 스케줄을 바꿨는데 위 맵을 안 고친 경우. 조용히 죽지 않도록 로그를 남긴다.
            print(f"[cron] 알 수 없는 스케줄: {schedule!r} — SCHEDULE_TO_WORKFLOW 확인 필요")
            self._reply(400, {"ok": False, "error": "unknown schedule", "schedule": schedule})
            return

        try:
            _dispatch(workflow)
        except HTTPError as e:
            # 401/403이면 PAT 만료·권한 부족, 404면 워크플로명이나 레포 오타.
            body = e.read().decode(errors="replace")[:300]
            print(f"[cron] {workflow} dispatch 실패: HTTP {e.code} {body}")
            self._reply(502, {"ok": False, "error": f"github {e.code}"})
            return
        except (URLError, RuntimeError, OSError) as e:
            print(f"[cron] {workflow} dispatch 실패: {type(e).__name__}: {e}")
            self._reply(502, {"ok": False, "error": str(e)})
            return

        self._reply(200, {"ok": True, "workflow": workflow})

    def _reply(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())

    def log_message(self, format, *args):
        pass
