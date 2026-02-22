"""하루봇 설정"""

# 실행 주기
PERIOD = "daily"
PERIOD_DAYS = 1

# 오늘 한 일 요약 개수
SUMMARY_COUNT = 3

# Claude 모델 (매일 실행이므로 비용 절약형)
CLAUDE_MODEL = "claude-sonnet-4-5-20250929"

# 요약 생성 최대 토큰
MAX_TOKENS = 1000

# Telegram 코멘트 대기 시간 (초)
TELEGRAM_POLL_TIMEOUT = 3600  # 1시간

# 모델별 가격 (USD per 1M tokens)
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
}
