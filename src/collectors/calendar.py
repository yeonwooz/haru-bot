"""Apple Calendar(iCloud CalDAV)에서 최근 일정 데이터를 수집하는 모듈"""

import os
from datetime import datetime, timedelta, timezone

import caldav


CALDAV_URL = "https://caldav.icloud.com"


def collect_calendar(period_days: int) -> list[dict]:
    """iCloud CalDAV를 통해 Apple Calendar에서 최근 일정을 수집한다.

    Args:
        period_days: 수집할 기간 (일 단위)

    Returns:
        [{"summary": str, "description": str, "start": str, "end": str}, ...]
    """
    apple_id = os.environ.get("APPLE_ID")
    apple_app_password = os.environ.get("APPLE_APP_PASSWORD")

    if not apple_id or not apple_app_password:
        print("[Calendar] APPLE_ID 또는 APPLE_APP_PASSWORD가 설정되지 않음 - 건너뜀")
        return []

    try:
        client = caldav.DAVClient(
            url=CALDAV_URL,
            username=apple_id,
            password=apple_app_password,
        )
        principal = client.principal()
        calendars = principal.calendars()
    except Exception as e:
        print(f"[Calendar] iCloud 연결 실패: {e}")
        return []

    now = datetime.now(timezone.utc)
    start = now - timedelta(days=period_days)
    results = []

    for cal in calendars:
        try:
            events = cal.search(
                start=start,
                end=now,
                event=True,
                expand=True,
            )
        except Exception as e:
            print(f"[Calendar] '{cal.name}' 검색 실패: {e}")
            continue

        for event in events:
            try:
                vevent = event.vobject_instance.vevent
                summary = str(vevent.summary.value) if hasattr(vevent, "summary") else "제목 없음"
                description = ""
                if hasattr(vevent, "description"):
                    description = str(vevent.description.value)[:200]

                dtstart = vevent.dtstart.value
                dtend = vevent.dtend.value if hasattr(vevent, "dtend") else dtstart

                results.append({
                    "summary": summary,
                    "description": description,
                    "start": str(dtstart),
                    "end": str(dtend),
                })
            except Exception:
                continue

    print(f"[Calendar] {len(results)}개 일정 수집 완료")
    return results
