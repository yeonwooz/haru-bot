"""하루봇 설정"""

# 실행 주기
PERIOD = "daily"
PERIOD_DAYS = 1

# 오늘 한 일 요약 개수
SUMMARY_COUNT = 3

# Claude 모델
CLAUDE_MODEL = "claude-opus-4-6"

# 요약 생성 최대 토큰
MAX_TOKENS = 1000

# Telegram 답장 대기 시간 (초) — GitHub Actions 제한 고려
TELEGRAM_REPLY_TIMEOUT = 300  # 5분

# 모델별 가격 (USD per 1M tokens)
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
}
