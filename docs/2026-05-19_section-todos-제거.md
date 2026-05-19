# 2026-05-19: `collect_section_todos` 제거, daily 페이지만 의존

## 배경

[2026-05-05](2026-05-05_날짜별-할일-페이지-수집.md)에서 daily TODO 페이지
(`YYYY-MM-DD TODO`) 수집을 추가하면서 기존 `collect_section_todos`와 결과를
dedupe로 합쳐 사용해 왔다.

확인 결과 notion-daily-todo가 `2026 할일` 페이지의 `# 날짜별 할 일` 아래에
매일 만드는 그 페이지에 반복 to-do(매일 아침 / 월요일마다 / 매월 1일 등)와
당일 캘린더·이월 항목을 이미 모두 채워주고 있었다. 즉 `collect_section_todos`
경로(헤더 LLM 판단 → 활성 sub-section만 골라 to-do 수집)는 redundant.

또한 `_claude_pick_active_headings`는 daily 페이지가 채워지면 동일한 항목을
다른 경로로 한 번 더 가져오는 형태라 Claude 호출 1회분이 그대로 낭비였다.

## 변경

### `src/collectors/notion.py`

- `collect_section_todos` 제거
- `_claude_pick_active_headings`, `_find_heading_index`, `_is_today_kst`
  헬퍼 제거 (전부 section_todos 전용)

### `src/collectors/__init__.py`

- `collect_section_todos` export 제거

### `src/main.py`

- `collect_section_todos` 호출 라인 제거
- `daily_uncompleted` / `daily_completed`를 곧바로
  `todo_uncompleted` / `todo_completed`로 사용 (section과의 dedupe 불필요)

### `config.py`

- `NOTION_TODO_PAGE_QUERY`, `NOTION_TODO_SECTION` 제거

## 영향

- Claude API 호출 1회 절약 (헤더 분류용)
- 반복 to-do 처리가 notion-daily-todo 쪽 로직 하나로 단일화됨
- `2026 할일` 페이지의 `# 할 일` 섹션을 수정해도 봇 동작에는 영향 없음
  (notion-daily-todo가 그 섹션을 어떻게 참고하는지가 단일 진실원)

## 검증

- `python -m py_compile` 통과 (4개 파일 syntax OK)
- 다음 GitHub Actions 실행에서 `[Notion-Daily]` 로그로 미완료/완료 개수 확인 예정
