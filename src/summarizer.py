"""Claude API를 사용하여 오늘 한 일 3가지를 요약하는 모듈"""

import os

import anthropic


SYSTEM_PROMPT = """당신은 사용자의 하루를 정리해주는 따뜻한 일기 도우미입니다.
사용자의 오늘 활동 데이터를 분석하여 두 섹션으로 정리합니다.

데이터 소스별 의미:
- 캘린더 일정: 시간이 있는 일정. done=true면 완료, false면 아직 안 한 일정
- Notion 작업 내용: 할일 DB 항목은 done 여부 확인, 일반 페이지는 작업한 내용
- GitHub 커밋: 실제로 한 작업 (항상 완료)

출력 형식:
[오늘의 일정]
캘린더 일정을 오전/오후/밤으로 나누어 정리. 완료된 항목은 ✓ 표시.
해당 시간대에 일정이 없으면 생략.

오전
- 일정 이름 ✓
- 일정 이름

오후
- 일정 이름 ✓

[태스크]
Notion 작업, GitHub 커밋 등 시간과 무관한 항목. 완료된 것은 ✓ 표시.

- 태스크 이름 ✓
- 태스크 이름

규칙:
- 캘린더 일정은 시간 정보를 활용해 오전(~12시)/오후(12~18시)/밤(18시~)으로 분류
- 완료 여부(done)를 정확히 반영하여 ✓ 표시
- 딱딱한 보고서가 아닌, 친근하고 자연스러운 톤으로 작성
- 데이터가 부족하면 있는 정보만으로 최선을 다해 정리
- 이모지는 ✓ 외에 사용하지 않음
- [오늘의 일정]과 [태스크] 헤더는 반드시 포함"""


def generate_summary(
    calendar_data: list[dict],
    notion_data: list[dict],
    model: str,
    max_tokens: int = 1000,
    github_data: list[dict] | None = None,
    user_settings: list[str] | None = None,
) -> tuple[str, dict]:
    """수집된 데이터를 바탕으로 오늘 한 일 3가지를 요약한다.

    Returns:
        (요약 텍스트, {"input_tokens": int, "output_tokens": int})
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(calendar_data, notion_data, github_data or [])

    system_prompt = SYSTEM_PROMPT
    if user_settings:
        settings_text = "\n".join(f"- {s}" for s in user_settings)
        system_prompt += f"\n\n사용자 지정 규칙 (반드시 따를 것):\n{settings_text}"
        print(f"[Summarizer] 사용자 설정 {len(user_settings)}건 적용")

    print(f"[Summarizer] Claude API 호출 중 (모델: {model})...")

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    result = message.content[0].text
    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    print(f"[Summarizer] 요약 완료 ({len(result)}자, 입력 {usage['input_tokens']}토큰, 출력 {usage['output_tokens']}토큰)")
    return result, usage


def _build_user_prompt(calendar_data: list[dict], notion_data: list[dict], github_data: list[dict] | None = None) -> str:
    """Claude에게 보낼 사용자 프롬프트를 구성한다."""
    sections = []

    if calendar_data:
        lines = []
        for item in calendar_data:
            done_mark = " (done=true)" if item.get("done") else " (done=false)"
            lines.append(f"- {item['start']} | {item['summary']}{done_mark}")
            if item["description"]:
                lines.append(f"  설명: {item['description']}")
        sections.append("### 캘린더 일정\n" + "\n".join(lines))

    if notion_data:
        lines = []
        for item in notion_data:
            tags = ", ".join(item["tags"]) if item["tags"] else ""
            tag_str = f" | 태그: {tags}" if tags else ""
            done_mark = " (done=true)" if item.get("done") else " (done=false)"
            lines.append(f"- {item['title']}{tag_str}{done_mark}")
            if item["excerpt"]:
                lines.append(f"  내용: {item['excerpt']}")
        sections.append("### Notion 작업/할일\n" + "\n".join(lines))

    if github_data:
        lines = []
        for item in github_data:
            lines.append(f"- [{item['repo']}] {item['message']} (done=true)")
        sections.append("### GitHub 커밋\n" + "\n".join(lines))

    if not sections:
        data_block = "(오늘 수집된 데이터가 없습니다. '오늘은 기록된 활동이 없어요. 직접 하루를 돌아봐 주세요!'라고 안내해주세요.)"
    else:
        data_block = "\n\n".join(sections)

    return f"""아래는 오늘 하루 동안의 활동 데이터입니다.

---
{data_block}
---

위 데이터를 [오늘의 일정]과 [태스크] 두 섹션으로 정리해주세요.
캘린더 일정은 시간대별(오전/오후/밤)로, 나머지는 태스크로 분류합니다.
완료 여부(done)를 확인하여 ✓ 표시를 정확히 붙여주세요."""
