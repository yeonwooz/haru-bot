"""Claude API를 사용하여 오늘 한 일 3가지를 요약하는 모듈"""

import os

import anthropic


SYSTEM_PROMPT = """당신은 사용자의 하루를 정리해주는 따뜻한 일기 도우미입니다.
사용자의 오늘 캘린더 일정을 시간대별로 정리합니다.

데이터 소스별 의미:
- 캘린더 일정: 시간이 있는 일정 (이것만 출력에 사용)
- Notion 작업 내용 / GitHub 커밋: 컨텍스트 참고용 (출력에는 포함하지 않음)

출력 형식:
오전
- 일정 이름
- 일정 이름

오후
- 일정 이름

밤
- 일정 이름

규칙:
- 캘린더 일정만 사용. 시간 정보로 오전(~12시)/오후(12~18시)/밤(18시~)으로 분류
- 해당 시간대에 일정이 없으면 그 시간대 헤더는 생략
- 완료/미완료 표시(✓)는 사용하지 않는다 (있던 일정 자체만 나열)
- 이모지·헤더 라벨([오늘의 일정] 같은 것)은 사용하지 않음
- 딱딱한 보고서가 아닌, 친근하고 자연스러운 톤
- 데이터가 부족하면 있는 정보만으로 정리"""


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


FEEDBACK_SYSTEM_PROMPT = """당신은 사용자의 하루를 함께 돌아보는 따뜻한 일기 도우미입니다.
사용자가 오늘 완료한 태스크와 못한 태스크를 보고, 3~5줄로 짧게 의견을 줍니다.

규칙:
- 친근하고 자연스러운 반말 톤
- 완료한 것이 있으면 구체적으로 짚어 칭찬
- 못한 것에 대해서는 자책하지 않게 위로하거나 가볍게 넘김
- 완료한 게 0개여도 격려 톤 유지 (오늘 하루 자체는 의미 있다는 뉘앙스)
- 이모지·헤더 사용 금지, 일반 문장으로만"""


def generate_feedback(
    completed: list[str],
    uncompleted: list[str],
    model: str,
    max_tokens: int = 400,
) -> tuple[str, dict]:
    """완료/미완료 태스크 리스트로 피드백 메시지를 생성한다."""
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)

    done_text = "\n".join(f"- {t}" for t in completed) if completed else "(없음)"
    undone_text = "\n".join(f"- {t}" for t in uncompleted) if uncompleted else "(없음)"
    user_prompt = (
        f"오늘 완료한 태스크:\n{done_text}\n\n"
        f"오늘 못한 태스크:\n{undone_text}\n\n"
        "위를 보고 3~5줄로 의견을 줘."
    )

    print(f"[Summarizer] 피드백 생성 중 (완료 {len(completed)}건, 미완료 {len(uncompleted)}건)")

    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=[{"type": "text", "text": FEEDBACK_SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user_prompt}],
    )

    result = message.content[0].text
    usage = {
        "input_tokens": message.usage.input_tokens,
        "output_tokens": message.usage.output_tokens,
    }
    print(f"[Summarizer] 피드백 생성 완료 ({len(result)}자, 입력 {usage['input_tokens']}토큰, 출력 {usage['output_tokens']}토큰)")
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

    return f"""아래는 오늘 하루 동안의 활동 데이터입니다. 캘린더 일정만 시간대별(오전/오후/밤)로 정리해 주세요.

---
{data_block}
---"""
