# 2026-05-23: 🏁 완료 시 daily TODO 페이지 체크 상태 동기화 + 피드백 프롬프트 정정

## 배경

회고봇 일기 페이지에서 to_do 체크박스를 갱신해도, notion-daily-todo가 만든
`YYYY-MM-DD TODO` 페이지의 체크 상태는 그대로였다. 사용자가 두 페이지를 모두
보기 때문에 한쪽만 갱신되는 건 혼란스럽다.

또 [2026-05-19](2026-05-19_section-todos-제거.md)에서 uncompleted를 더 이상
입력으로 안 넣게 됐는데, `_generate_feedback` 시그니처와 시스템 프롬프트는
여전히 "못한 태스크"를 받는 전제로 남아 있었다. 모델에게 없는 정보를
짚으라고 시키고 있던 셈.

## 변경

### `api/webhook.py`

- `_generate_feedback(completed, uncompleted)` → `_generate_feedback(completed)`
- 시스템/유저 프롬프트에서 "못한 태스크" 라인 제거, "못한 태스크는 언급하지 않기"
  규칙 추가 (입력으로 안 주어진다는 점 명시)
- `_get_page_date` 추가 — 일기 페이지의 `date` 컬럼(YYYY-MM-DD)을 읽음
- `_find_daily_todo_page_id` 추가 — `client.search`로 `{date} TODO` 제목 정확
  매치 페이지 찾기 (notion-client 3.x는 `databases.query` 없음 → search 사용)
- `_sync_todos_in_daily_page` 추가 — daily 페이지 to_do 블록을 일기 페이지의
  `(text, checked)` 리스트에 맞춰 일괄 갱신. 매칭은 trimmed text equality.
- `_handle_done`에서 🏁 완료 직전에 1회 호출 (각 토글마다 X)

## 동기화 정책

- 매칭 실패(이름 불일치, daily 페이지 없음, nested to_do)는 silent — 일기 쪽이
  source of truth고 daily는 보조 뷰라는 전제
- 이미 원하는 상태인 블록은 update 호출 스킵 (API 호출 절약)
- pagination 처리 (`has_more` / `next_cursor`)

## 영향

- 일기 페이지에서 한 번 체크하면 daily TODO 페이지에도 즉시 반영
- 피드백 품질: "못한 거 없네!" 같은 환각성 멘트가 사라질 것으로 예상

## 검증

- `python -m py_compile api/webhook.py` 통과
- 실제 동작은 다음 봇 실행(오후 8시 KST) + 텔레그램 🏁 완료 시 확인 예정
