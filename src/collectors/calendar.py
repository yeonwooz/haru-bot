"""Apple Calendar(iCloud CalDAV)에서 최근 일정 데이터를 수집하는 모듈"""

import os
from datetime import datetime, timedelta, timezone

import caldav


CALDAV_URL = "https://caldav.icloud.com"


def collect_calendar(period_days: int, anchor: datetime | None = None) -> list[dict]:
    """iCloud CalDAV를 통해 Apple Calendar에서 최근 일정을 수집한다.

    Args:
        period_days: 수집할 기간 (일 단위)
        anchor: "오늘"의 기준 시각 (기본: 현재 KST). cron 지연으로 자정을 넘겨
            실행돼도 의도한 날짜의 일정을 수집할 수 있도록 호출부에서 보정해 전달.

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

    KST = timezone(timedelta(hours=9))
    now_kst = anchor if anchor is not None else datetime.now(KST)
    today_date = now_kst.date()
    start = now_kst.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    results = []
    dropped = 0

    for cal in calendars:
        try:
            events = cal.search(
                start=start,
                end=end,
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

                # 종일 일정은 날짜만 있는 floating 값이라, caldav가 검색창을 UTC로
                # 변환할 때 인접한 날(주로 어제) 일정이 오늘 창에 섞여 들어온다.
                # 실제 일정 날짜가 KST 오늘에 걸치는지 명시적으로 한 번 더 거른다.
                if not _event_on_kst_day(dtstart, dtend, today_date, KST):
                    dropped += 1
                    continue

                # 일정이 지났다고 실제로 했다는 뜻은 아니므로 완료 여부는 판정하지 않는다.
                # "한 일"은 사용자가 직접 체크한 항목에서만 나온다 (2026-07-04 만두 사건).
                results.append({
                    "summary": summary,
                    "description": description,
                    "start": str(dtstart),
                    "end": str(dtend),
                })
            except Exception:
                continue

    msg = f"[Calendar] {len(results)}개 일정 수집 완료"
    if dropped:
        msg += f" (KST 오늘 아닌 {dropped}개 제외)"
    print(msg)
    return results


def _event_on_kst_day(dtstart, dtend, day, kst) -> bool:
    """일정이 KST 기준 `day`(date)에 걸치는지 판정.

    - 시간 지정 일정(datetime): KST로 변환한 시작~종료 날짜 구간이 day를 포함
    - 종일 일정(date): iCal에서 dtend는 배타적(다음날) → [dtstart, dtend) 반열림 구간
    """
    if hasattr(dtstart, "hour"):  # 시간 지정
        start_d = dtstart.astimezone(kst).date()
        end_d = dtend.astimezone(kst).date() if hasattr(dtend, "hour") else start_d
        return start_d <= day <= end_d
    end_date = dtend if (dtend and dtend > dtstart) else dtstart + timedelta(days=1)
    return dtstart <= day < end_date
