"""하루봇 설정"""

# 실행 주기
PERIOD = "daily"
PERIOD_DAYS = 1

# 저녁 일기 예약 시각 (KST) — .github/workflows/daily.yml cron(UTC 12:00)과 맞출 것.
# main.py가 "가장 최근 예약 시각"으로 대상 날짜를 정하는 기준.
DAILY_RUN_HOUR_KST = 21

# 오늘 한 일 요약 개수
SUMMARY_COUNT = 3

# Claude 모델
CLAUDE_MODEL = "claude-opus-4-6"

# 요약 생성 최대 토큰 (실제 과금은 사용량 기준; 잘림 방지용 상한)
MAX_TOKENS = 16000

# Telegram 답장 대기 시간 (초) — GitHub Actions 제한 고려
TELEGRAM_REPLY_TIMEOUT = 300  # 5분

# 모델별 가격 (USD per 1M tokens)
MODEL_PRICING = {
    "claude-opus-4-6": {"input": 15.0, "output": 75.0},
    "claude-sonnet-4-5-20250929": {"input": 3.0, "output": 15.0},
    "claude-sonnet-4-6": {"input": 3.0, "output": 15.0},
    "gemini-2.5-flash": {"input": 0.30, "output": 2.50},  # Claude 장애 시 폴백
}
