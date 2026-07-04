# 2026-07-04 notion-daily-todo 통합 + 21시 이동 + Claude 재시도/Gemini 폴백

## 배경

notion-daily-todo(06:00 TODO 페이지 생성)와 haru-bot은 `{date} TODO` 페이지와
`🔜 내일로` 섹션을 공유하는 사실상 한 파이프라인인데 repo가 나뉘어 있어,
같은 캘린더 버그를 양쪽에서 두 번 고치는 등(haru `e882e22` / ndt `a113950`)
중복 관리 비용이 커서 haru-bot으로 흡수 통합.

## 변경 내용

### 1. `src/daily_todo.py` 신설 (notion-daily-todo에서 이식)

- `create_daily_todo.py`를 그대로 이식 + haru-bot 스타일 적용 (`load_dotenv`, sys.path shim)
- 페이지 생성 직후 `src/morning_todo.py`의 `run()`을 호출해 **아침 알림까지 한 번에 발송**
  (기존: 06:00 생성 / 07:00 알림 별도 워크플로우 → 이제 06:00에 생성+알림)
- `--dry-run` 플래그 추가: Notion 페이지 생성/텔레그램 발송 없이 구성 결과만 출력
- Notion 호출은 기존 urllib 방식 유지 (notion-client 통일은 추후 리팩터링)
- caldav는 haru-bot lock에 이미 2.2.6이라 icalendar API 그대로 동작

### 2. 워크플로우

- `.github/workflows/daily-todo.yml` 신설 — 매일 UTC 21:00 (= KST 06:00), 생성+알림
- `.github/workflows/morning-todo.yml` 삭제 (daily-todo.yml에 통합)
- `.github/workflows/daily.yml` — cron `0 13` → `0 12` (**저녁 일기 22시 → 21시 KST**)
  - 기존에 docstring/문서는 "오후 8시", 실제 cron은 22시로 불일치했음 → 21시로 통일
  - `src/main.py` docstring, `diagram.md`도 21시(밤 9시)로 갱신

### 3. 환경변수

- `.env` / `.env.example`에 `ROOT_PAGE_ID`, `TEMPLATE_PAGE_ID` 추가
- `.env.example`에 `GEMINI_API_KEY` 추가 (폴백용, 선택)

### 4. Claude 재시도 + Gemini 폴백 (`src/llm.py` 신설)

Actions 스크립트의 Claude 호출 5곳(daily_todo 필터/분류/2차복구, summarizer 요약/dedupe)을
`llm.generate()` 래퍼로 통일:

1. Claude 1차 시도 — 모델은 `config.CLAUDE_MODEL`(opus-4-6)로 통일
   (분류가 sonnet-4-6이었으나 누락이 잦아 opus로 변경, 비용 차이 미미)
2. anthropic SDK 자동 재시도 최대 2회 (`max_retries=2`, 연결오류/408/429/5xx만)
3. 실패 시 `GEMINI_API_KEY` 있으면 `gemini-2.5-flash`로 동일 요청
   (JSON 스키마는 `responseSchema`로 유지, thinking off, urllib 직접 호출 — 의존성 추가 없음)
4. Gemini도 실패/키 없음 → 예외 전파 → 기존 안전장치(필터 생략, 첫 카테고리 폴백, 원본 반환)
5. 폴백 발생 시 usage_log.csv에 실제 모델(`gemini-2.5-flash`)과 note "gemini 폴백" 기록

검증: 정상 dry-run 통과 + 잘못된 키 주입 시 "Claude 실패 → Gemini 폴백" 전환 확인.
webhook(Vercel)은 이번 범위 제외.

### 5. 분류기 인덱스 기반 재작성 + opus 통일 (daily_todo.py)

- opus 테스트 중 모델이 일정 텍스트를 변형("[11:00]"→"[10:00]")해 같은 일정이 다른
  시간으로 중복 등록되는 문제 발견 → **항목을 번호로 제시하고 모델은 번호만 고르는
  방식**으로 재작성. 텍스트 변형 원천 차단, 누락은 코드에서 결정적으로 첫 카테고리 폴백
- 2차 복구 LLM 호출(`_recover_missing_items`) 삭제 — 필요 없어짐
- 분류/필터 모델을 `config.CLAUDE_MODEL`(opus-4-6)로 통일 (sonnet의 분류 누락이 잦았음)
- 경민 필터에 "경민과 사용자가 함께하는 일정은 keep" 규칙 추가 (공동 일정 오제외 방지)

### 6. 자정 넘김 날짜 보정 (main.py, collectors/calendar.py)

- **사고**: 7/3 밤 daily run이 GitHub cron 지연으로 UTC 15:02(= KST 7/4 00:02)에 실행
  → "오늘"이 7/4로 잡혀 빈 7/4 일기가 새벽에 생기고 7/3 일기는 누락
- **수정**: "가장 최근에 도래한 예약 시각(매일 21:00 KST, `config.DAILY_RUN_HOUR_KST`)"을
  anchor로 사용 — cron이 **최대 24시간 지연돼도 원래 의도한 날짜**가 나오고, 그 이상이면
  다음 예약분이 도래한 것이므로 다음 날짜. (처음엔 -6h 상대 오프셋으로 구현했다가
  6시간 이상 지연에서 다시 밀리는 문제가 있어 절대 예약시각 방식으로 교체)
- `python src/main.py --date 2026-07-03` 형태로 누락된 날짜 백필 가능
- `collect_calendar(period_days, anchor=None)` 파라미터 추가

### 7. webhook: `/일기` prefix (api/webhook.py — master push 시 Vercel 자동 배포)

- **변경 전**: 아무 텍스트나 보내면 일기 discussion에 저장 (+오늘 일기 없으면 날짜 질문)
- **변경 후**: `/일기 내용`일 때만 일기에 저장·날짜 질문. prefix 없는 텍스트는
  **저장 없이 클로드와 대화만** (오늘 일기가 있으면 읽기 전용 컨텍스트로 활용)
- `/` 단독, 내용 없는 `/일기`·`/설정`, 모르는 `/명령` → 사용법 안내(HELP_TEXT) 응답
- dedupe(summarizer)도 인덱스+스키마 강제 출력으로 전환 — 백필 때 발생한 JSON 파싱
  실패류가 원천 차단됨 (남길 번호만 반환, 출력 ~17토큰)

### 8. GitHub 커밋 수집 부활 (daily.yml)

- 저녁 일기의 GitHub 커밋 수집(`collectors/github.py`)이 GITHUB_TOKEN 미주입으로
  Actions에서 한 번도 동작한 적 없었음 → **Actions 자동 토큰** 한 줄 추가로 해결
  (`GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}`, 별도 발급 불필요, public repo만)
- 회사 git 계정은 수집하지 않기로 결정 (개인 `yeonwooz` 계정만)
- 로컬 검증: gh CLI 토큰으로 최근 3일 커밋 1건 정상 수집

## 검증

`uv run python src/daily_todo.py --dry-run` (로컬, 2026-07-04):
- 연/월 페이지 확보, 템플릿 카테고리(일/생활/공부) 인식 OK
- 캘린더 7개 수집, 경민 일정 1개 제외(미용실 — 같이 가는 커리어코칭은 유지) OK
- 분류 1차에서 2개 누락 → 2차 폴백으로 전량 배치 OK
- Windows 콘솔(cp949)에서 dry-run 출력의 `☐` 인코딩 에러 → `[ ]`로 교체

## 남은 일 (전환 절차)

1. haru-bot repo secrets에 `ROOT_PAGE_ID`, `TEMPLATE_PAGE_ID` 등록 (자동 승인 거부되어 수동 필요)
2. `GEMINI_API_KEY` 발급(aistudio.google.com/apikey) → .env + repo secret (없어도 동작, 폴백만 비활성)
3. haru-bot 커밋/푸시
4. **notion-daily-todo의 daily-todo 워크플로우 비활성화** — 안 하면 06:00에 페이지 중복 생성
5. notion-daily-todo repo archive

## 추후 리팩터링 후보

- `src/collectors/calendar.py`가 아직 vobject API 사용 (daily_todo.py는 icalendar API) — 통일
- daily_todo의 Notion urllib 호출을 notion-client로 통일
- daily_todo의 Claude 호출(sonnet-4-6 분류/필터)도 usage_log.csv에 기록
