"""하루봇 설정"""

# 실행 주기
PERIOD = "daily"
PERIOD_DAYS = 1

# 오늘 한 일 요약 개수
SUMMARY_COUNT = 3

# Claude 모델
CLAUDE_MODEL = "claude-opus-4-6"

# 요약 생성 최대 토큰 (실제 과금은 사용량 기준; 잘림 방지용 상한)
MAX_TOKENS = 16000

# Telegram 답장 대기 시간 (초) — GitHub Actions 제한 고려
TELEGRAM_REPLY_TIMEOUT = 300  # 5분

# 노션 "할 일" 페이지 — 매일 미완료 to-do를 태스크 키보드에 추가하는 소스
NOTION_TODO_PAGE_QUERY = "2026 할일"
NOTION_TODO_SECTION = "할 일"

# 모델별 가격 (USD per 1M tokens)
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
}
