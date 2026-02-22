"""GitHub에서 오늘 커밋 데이터를 수집하는 모듈"""

import os
from datetime import datetime, timedelta, timezone

import httpx

KST = timezone(timedelta(hours=9))
GITHUB_API = "https://api.github.com"
# TODO: 회사 git 계정도 수집하기
# TODO: GITHUB_TOKEN 발급 및 .env, GitHub Secrets 등록
GITHUB_USER = "yeonwooz"


def collect_github(period_days: int) -> list[dict]:
    """GitHub에서 최근 커밋을 수집한다.

    Args:
        period_days: 수집할 기간 (일 단위)

    Returns:
        [{"repo": str, "message": str, "time": str}, ...]
    """
    token = os.environ.get("GITHUB_TOKEN")

    if not token:
        print("[GitHub] GITHUB_TOKEN이 설정되지 않음 - 건너뜀")
        return []

    today = datetime.now(KST).strftime("%Y-%m-%d")
    since = (datetime.now(KST) - timedelta(days=period_days)).strftime("%Y-%m-%d")

    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github.v3+json",
    }

    query = f"author:{GITHUB_USER} committer-date:{since}..{today}"

    try:
        resp = httpx.get(
            f"{GITHUB_API}/search/commits",
            params={"q": query, "sort": "committer-date", "order": "desc", "per_page": 50},
            headers=headers,
            timeout=30,
        )
        resp.raise_for_status()
    except Exception as e:
        print(f"[GitHub] API 호출 실패: {e}")
        return []

    data = resp.json()
    results = []

    for item in data.get("items", []):
        commit = item.get("commit", {})
        repo_name = item.get("repository", {}).get("full_name", "")
        message = commit.get("message", "").split("\n")[0]  # 첫 줄만
        committer_date = commit.get("committer", {}).get("date", "")

        results.append({
            "repo": repo_name,
            "message": message,
            "time": committer_date,
        })

    print(f"[GitHub] {len(results)}개 커밋 수집 완료")
    return results
