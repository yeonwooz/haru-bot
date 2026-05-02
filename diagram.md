# haru-bot Architecture

## 전체 파이프라인

매일 실행되는 `main.py`는 한 방향(수집 → 요약 → 전송 → 저장)으로만 흐른다.
사용자 답장은 Vercel webhook(`api/webhook.py`)이 비동기로 받아 직접 Notion에 반영한다.

```mermaid
flowchart TD
    subgraph Trigger
        GHA["GitHub Actions<br/>매일 오후 8시 KST"]
    end

    GHA --> MAIN["main.py :: run()"]

    MAIN --> STEP0["0. ensure_setting_column()<br/>Notion DB에 setting 컬럼 확보"]
    STEP0 --> STEP1

    subgraph STEP1["1. 데이터 수집 (collectors)"]
        CAL["calendar.py<br/>iCloud CalDAV"]
        NOTION_C["notion.py<br/>Notion Search API"]
        GH["github.py<br/>GitHub Search API"]
    end

    STEP1 --> STEP2

    subgraph STEP2["2. 요약 생성"]
        SETTINGS["diary_store.load_settings()<br/>사용자 설정 로드"]
        SUMMARIZER["summarizer.py<br/>Claude API 호출"]
        SETTINGS --> SUMMARIZER
    end

    STEP2 --> STEP3["3. Telegram 전송<br/>send_summary() + send_checklist()"]
    STEP3 --> STEP4["4. 일기 저장<br/>diary_store.save_diary()"]
    STEP4 --> STEP5["5. usage_log.csv 기록"]
```

## 사용자 답장 흐름 (비동기)

```mermaid
flowchart LR
    USER["사용자"] -->|"코멘트 / /설정 X"| TG["Telegram"]
    TG -->|"webhook POST"| VERCEL["Vercel<br/>api/webhook.py"]

    VERCEL --> ROUTE{메시지 종류}
    ROUTE -->|"callback_query"| CB["체크리스트 버튼<br/>→ editMessageText"]
    ROUTE -->|"text message"| CLASSIFY{prefix?}
    CLASSIFY -->|"/설정 / /set"| SET["오늘 일기<br/>setting append"]
    CLASSIFY -->|"일반 텍스트"| CMT["오늘 일기<br/>comment append"]

    SET --> NOTION["Notion 일기 DB"]
    CMT --> NOTION
    SET -.->|"확인 답장"| TG
    CMT -.->|"확인 답장"| TG
```

## 모듈 의존 관계

```mermaid
graph LR
    MAIN["main.py"] --> COLLECTORS["collectors/"]
    MAIN --> SUMMARIZER["summarizer.py"]
    MAIN --> TELEGRAM["telegram_bot.py"]
    MAIN --> DIARY["diary_store.py"]
    MAIN --> CONFIG["config.py"]

    COLLECTORS --> CAL["calendar.py<br/>caldav"]
    COLLECTORS --> NOT["notion.py<br/>notion-client"]
    COLLECTORS --> GH["github.py<br/>httpx"]

    SUMMARIZER --> ANTHROPIC["anthropic"]
    TELEGRAM --> TG["python-telegram-bot"]
    DIARY --> NOTION_SDK["notion-client"]

    WEBHOOK["api/webhook.py<br/>(Vercel)"] --> NOTION_SDK
    WEBHOOK -.->|"urllib"| TG_API["Telegram Bot API"]
```

## 외부 서비스 연동

```mermaid
graph TB
    BOT["haru-bot (main.py)"]
    WEBHOOK["haru-bot (webhook)"]

    BOT -->|"CalDAV"| ICLOUD["iCloud Calendar"]
    BOT -->|"Search API"| NOTION["Notion"]
    BOT -->|"Search Commits API"| GITHUB["GitHub"]
    BOT -->|"Messages API"| CLAUDE["Claude API<br/>claude-opus-4-6"]
    BOT -->|"sendMessage"| TELEGRAM["Telegram"]

    TELEGRAM -->|"webhook POST"| WEBHOOK
    WEBHOOK -->|"pages.update"| NOTION
    WEBHOOK -->|"sendMessage<br/>editMessageText"| TELEGRAM
```

## 데이터 흐름

```mermaid
flowchart LR
    subgraph 수집
        IC["iCloud 일정"]
        NO["Notion 페이지"]
        GH["GitHub 커밋"]
    end

    subgraph 처리
        CLAUDE["Claude API"]
    end

    subgraph 출력
        TG["Telegram 메시지"]
        DIARY["Notion 일기 DB"]
        LOG["usage_log.csv"]
    end

    IC --> CLAUDE
    NO --> CLAUDE
    GH --> CLAUDE
    CLAUDE -->|"오늘 한 일 요약"| TG
    CLAUDE -->|"summary"| DIARY
    TG -->|"답장 → webhook"| DIARY
    CLAUDE -->|"토큰 사용량"| LOG
```
