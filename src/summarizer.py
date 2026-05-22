"""Claude API를 사용하여 오늘 한 일 3가지를 요약하는 모듈"""

import json
import os
import re

import anthropic


SYSTEM_PROMPT = """당신은 사용자의 하루를 정리해주는 긍정적이고 합리적인 일기 도우미입니다.
사용자의 오늘 캘린더 일정과 오늘 완료한 태스크를 자연스럽게 한 글에 녹여 정리합니다.

데이터 소스별 의미:
- 캘린더 일정: 시간이 있는 일정 (오전/오후/밤으로 분류)
- 오늘 완료한 태스크: 노션 "할 일" 페이지에서 오늘 체크된 항목들 (시간 무관)

출력 형식 예시:
오전
- 일정 이름
- 일정 이름

오후
- 일정 이름

밤
- 일정 이름

오늘 한 일
- 완료한 태스크 이름
- 완료한 태스크 이름

규칙:
- 캘린더 일정은 시간 정보로 오전(~12시)/오후(12~18시)/밤(18시~)으로 분류
- 해당 시간대에 일정이 없으면 그 시간대 헤더는 생략
- 완료한 태스크는 "오늘 한 일" 헤더 아래로 모음. 없으면 "오늘 한 일" 헤더 자체 생략
- 완료/미완료 표시(✓) 같은 마크는 사용하지 않는다
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
    completed_today: list[str] | None = None,
) -> tuple[str, dict]:
    """수집된 데이터를 바탕으로 오늘 한 일 3가지를 요약한다.

    Returns:
        (요약 텍스트, {"input_tokens": int, "output_tokens": int})
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY가 설정되지 않았습니다.")

    client = anthropic.Anthropic(api_key=api_key)
    user_prompt = _build_user_prompt(calendar_data, notion_data, github_data or [], completed_today or [])

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


DEDUPE_SYSTEM_PROMPT = """사용자의 오늘 미완료 todo 리스트에서 의미상 중복되는 항목을 찾아 그룹별로 대표 하나만 남긴다.

판단 기준:
- 어미·조사·공백 차이만 있는 항목은 같은 항목 ("회의 준비" = "회의 준비하기")
- 한쪽이 다른 쪽을 명백히 포함하면 같은 항목 ("스탠드업" = "팀 스탠드업")
- 구체 대상이 다르면 별개 ("PR 리뷰" ≠ "리뷰: feat/foo")
- 의심스러우면 별개로 둠 (false-positive 병합이 누락보다 나쁨)

대표는 입력 문구 중 하나를 그대로 선택한다. 재작성·요약하지 않는다.

출력은 JSON 배열 하나만. 다른 텍스트·코드펜스·설명 금지.
예) ["회의 준비", "PR 리뷰"]"""


def dedupe_tasks(items: list[str], model: str) -> tuple[list[str], dict]:
    """의미상 중복되는 todo 항목을 Claude로 판별해 그룹별 대표만 남긴다.

    실패하거나 응답이 입력의 부분집합이 아니면 원본 그대로 반환한다 (silent).
    Returns: (deduped_items, {"input_tokens": int, "output_tokens": int})
    """
    empty_usage = {"input_tokens": 0, "output_tokens": 0}
    if len(items) < 2:
        return items, empty_usage

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("[Dedupe] ANTHROPIC_API_KEY 미설정, 원본 반환")
        return items, empty_usage

    user_prompt = "입력 todo 리스트:\n" + "\n".join(f"- {t}" for t in items)

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=1000,
            system=DEDUPE_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        text = message.content[0].text.strip()
        usage = {
            "input_tokens": message.usage.input_tokens,
            "output_tokens": message.usage.output_tokens,
        }
    except Exception as e:
        print(f"[Dedupe] Claude 호출 실패: {e}, 원본 반환")
        return items, empty_usage

    # 응답에서 JSON 배열만 추출 (혹시 텍스트가 섞여도)
    match = re.search(r"\[.*\]", text, re.DOTALL)
    if not match:
        print(f"[Dedupe] JSON 배열 못 찾음, 원본 반환: {text[:100]!r}")
        return items, usage

    try:
        kept = json.loads(match.group(0))
    except json.JSONDecodeError as e:
        print(f"[Dedupe] JSON 파싱 실패: {e}, 원본 반환")
        return items, usage

    if not isinstance(kept, list) or not all(isinstance(x, str) for x in kept):
        print(f"[Dedupe] 응답이 str 배열이 아님, 원본 반환")
        return items, usage

    # 안전장치: kept는 입력의 부분집합이어야 함 (Claude가 재작성하면 거부)
    items_set = set(items)
    if not all(x in items_set for x in kept):
        unknown = [x for x in kept if x not in items_set]
        print(f"[Dedupe] 응답에 입력 외 항목 포함, 원본 반환: {unknown[:3]}")
        return items, usage

    # 입력 순서 유지하면서 kept에 있는 것만 남김
    kept_set = set(kept)
    result = [x for x in items if x in kept_set]
    removed = len(items) - len(result)
    if removed > 0:
        print(f"[Dedupe] {removed}개 의미 중복 제거 ({len(items)}→{len(result)})")
    else:
        print(f"[Dedupe] 중복 없음 ({len(items)}개)")
    return result, usage


def _build_user_prompt(
    calendar_data: list[dict],
    notion_data: list[dict],
    github_data: list[dict] | None = None,
    completed_today: list[str] | None = None,
) -> str:
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

    if completed_today:
        lines = [f"- {t}" for t in completed_today]
        sections.append("### 오늘 완료한 태스크 (노션 할 일 페이지)\n" + "\n".join(lines))

    if not sections:
        data_block = "(오늘 수집된 데이터가 없습니다. '오늘은 기록된 활동이 없어요. 직접 하루를 돌아봐 주세요!'라고 안내해주세요.)"
    else:
        data_block = "\n\n".join(sections)

    return f"""아래는 오늘 하루 동안의 활동 데이터입니다. 캘린더 일정만 시간대별(오전/오후/밤)로 정리해 주세요.

---
{data_block}
---"""
