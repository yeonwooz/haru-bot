"""LLM 호출 공통 래퍼 — Claude 재시도 후에도 실패하면 Gemini로 폴백

- Claude: anthropic SDK가 연결 오류/408/429/5xx를 지수 백오프로 자동 재시도 (max_retries=2)
- 재시도 후에도 실패하면 (또는 인증 오류 등 즉시 실패하면) GEMINI_API_KEY가 있을 때
  같은 요청을 Gemini로 재시도. 키가 없으면 원래 예외를 그대로 올림 → 호출부의
  기존 폴백(필터 생략, 첫 카테고리 배치 등)이 동작
- schema를 주면 양쪽 모두 JSON 스키마 강제 출력
  (Claude: output_config.format.json_schema / Gemini: responseSchema)
"""

import json
import os
import urllib.request

import anthropic

GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
GEMINI_API_BASE = "https://generativelanguage.googleapis.com/v1beta"


def generate(
    prompt: str,
    *,
    model: str,
    max_tokens: int = 1024,
    system: str | None = None,
    schema: dict | None = None,
) -> tuple[str, dict]:
    """텍스트를 생성한다.

    Returns:
        (text, usage) — usage = {"input_tokens", "output_tokens", "model"(실제 사용 모델)}
    """
    try:
        return _call_claude(prompt, model=model, max_tokens=max_tokens, system=system, schema=schema)
    except Exception as e:
        if not os.environ.get("GEMINI_API_KEY"):
            print(f"[LLM] Claude 실패({type(e).__name__}: {e}) — GEMINI_API_KEY 미설정, 폴백 불가")
            raise
        print(f"[LLM] Claude 실패({type(e).__name__}: {e}) — Gemini({GEMINI_MODEL})로 폴백")
        return _call_gemini(prompt, max_tokens=max_tokens, system=system, schema=schema)


def _call_claude(prompt, *, model, max_tokens, system, schema):
    # max_retries=2: SDK 기본값이지만 "2회 재시도" 의도를 명시
    client = anthropic.Anthropic(max_retries=2)
    kwargs = {}
    if system:
        kwargs["system"] = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    if schema:
        kwargs["output_config"] = {"format": {"type": "json_schema", "schema": schema}}
    resp = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
        **kwargs,
    )
    text = next(b.text for b in resp.content if b.type == "text")
    usage = {
        "input_tokens": resp.usage.input_tokens,
        "output_tokens": resp.usage.output_tokens,
        "model": model,
    }
    return text, usage


def _strip_unsupported_keys(schema):
    """Gemini responseSchema가 지원하지 않는 additionalProperties 키를 재귀 제거."""
    if isinstance(schema, dict):
        return {k: _strip_unsupported_keys(v) for k, v in schema.items() if k != "additionalProperties"}
    if isinstance(schema, list):
        return [_strip_unsupported_keys(x) for x in schema]
    return schema


def _call_gemini(prompt, *, max_tokens, system, schema):
    generation_config = {
        "maxOutputTokens": max_tokens,
        # 폴백은 저비용·짧은 출력 위주라 thinking을 꺼서 max_tokens 잠식 방지
        "thinkingConfig": {"thinkingBudget": 0},
    }
    if schema:
        generation_config["responseMimeType"] = "application/json"
        generation_config["responseSchema"] = _strip_unsupported_keys(schema)

    body = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": generation_config,
    }
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}

    req = urllib.request.Request(
        f"{GEMINI_API_BASE}/models/{GEMINI_MODEL}:generateContent",
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json; charset=utf-8",
            "x-goog-api-key": os.environ["GEMINI_API_KEY"],
        },
        method="POST",
    )
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    text = "".join(
        p.get("text", "") for p in data["candidates"][0]["content"].get("parts", [])
    )
    meta = data.get("usageMetadata", {})
    usage = {
        "input_tokens": meta.get("promptTokenCount", 0),
        "output_tokens": meta.get("candidatesTokenCount", 0),
        "model": GEMINI_MODEL,
    }
    return text, usage
