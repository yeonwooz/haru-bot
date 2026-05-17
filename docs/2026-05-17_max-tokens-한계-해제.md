# 2026-05-17: max_tokens 한계 해제

## 배경

봇이 보내는 응답이 중간에 잘려서 오는 경우가 있었다. 코드 곳곳에 박혀
있던 max_tokens 값이 너무 빡빡하게 잡혀 있었다.

- `config.py` `MAX_TOKENS = 1000` — 일간 요약
- `api/webhook.py` `FEEDBACK_MAX_TOKENS = 400` — 완료/미완료 태스크 피드백
- `api/webhook.py` `REPLY_MAX_TOKENS = 300` — discussion 대화 답장
- `api/webhook.py` `DATE_PARSE_MAX_TOKENS = 80` — 날짜 파싱 JSON
- `src/collectors/notion.py` `max_tokens=400` — 활성 헤더 선별

`max_tokens`는 출력 상한이고 실제 과금은 사용량 기준이므로, 상한을 올려도
응답이 짧으면 비용 영향이 없다. 잘림만 사라진다.

## 변경

전부 `16000`으로 통일. Opus 4.6 출력 한계(32K) 안쪽이라 안전하다.

- `config.py:14` `MAX_TOKENS = 16000`
- `api/webhook.py:35-37` `FEEDBACK_MAX_TOKENS`, `REPLY_MAX_TOKENS`,
  `DATE_PARSE_MAX_TOKENS` 전부 `16000`
- `src/collectors/notion.py:273` `max_tokens=16000`

## 남은 이슈

텔레그램 자체에 메시지당 4096자 제한이 있는데 `_telegram_api("sendMessage", ...)`
에 분할 처리가 없다. 모델 출력이 4096자를 넘기면 텔레그램에서 400 에러가
날 수 있다. 실제로 그렇게 길게 나올 일은 드물어서 이번엔 손대지 않았고,
잘려서 들어오는 사례가 생기면 분할 처리를 추가하기로 한다.
