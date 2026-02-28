"""Claude API를 사용하여 오늘 한 일 3가지를 요약하는 모듈"""

import os

import anthropic


SYSTEM_PROMPT = """당신은 사용자의 하루를 정리해주는 따뜻한 일기 도우미입니다.
사용자의 오늘 활동 데이터를 분석하여,
오늘 실제로 한 일 중 가장 의미 있는 3가지를 골라 자연스러운 한국어로 정리합니다.

데이터 소스별 의미:
- 캘린더 일정: 오늘 있었던 일정. 대부분 실제로 참석한 것으로 간주
- Notion 작업 내용: 실제 작업한 내용 (할일 목록은 완료된 것만 포함됨)
- GitHub 커밋: 실제로 한 작업

출력 형식:
1. **[한 일 제목]** - 무엇을 했는지 1~2문장으로 설명
2. **[한 일 제목]** - 무엇을 했는지 1~2문장으로 설명
3. **[한 일 제목]** - 무엇을 했는지 1~2문장으로 설명

규칙:
- 캘린더, Notion, GitHub 모든 소스를 동등하게 고려
- 딱딱한 보고서가 아닌, 친근하고 자연스러운 톤으로 작성
- 단순 나열이 아니라, 하루의 흐름이 느껴지도록 구성
- 데이터가 부족하면 있는 정보만으로 최선을 다해 정리
- 이모지는 사용하지 않음"""


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
            lines.append(f"- {item['start']} | {item['summary']}")
            if item["description"]:
                lines.append(f"  설명: {item['description']}")
        sections.append("### 오늘 캘린더 일정\n" + "\n".join(lines))

    if notion_data:
        lines = []
        for item in notion_data:
            tags = ", ".join(item["tags"]) if item["tags"] else ""
            tag_str = f" | 태그: {tags}" if tags else ""
            lines.append(f"- {item['title']}{tag_str}")
            if item["excerpt"]:
                lines.append(f"  내용: {item['excerpt']}")
        sections.append("### 오늘 Notion에서 작업한 내용\n" + "\n".join(lines))

    if github_data:
        lines = []
        for item in github_data:
            lines.append(f"- [{item['repo']}] {item['message']}")
        sections.append("### 오늘 GitHub 커밋\n" + "\n".join(lines))

    if not sections:
        data_block = "(오늘 수집된 데이터가 없습니다. '오늘은 기록된 활동이 없어요. 직접 하루를 돌아봐 주세요!'라고 안내해주세요.)"
    else:
        data_block = "\n\n".join(sections)

    return f"""아래는 오늘 하루 동안의 활동 데이터입니다.

---
{data_block}
---

위 데이터를 분석하여, 오늘 한 일 중 가장 의미 있는 **3가지**를 골라 정리해주세요."""
