"""
LLM 调用 + JSON 解析兜底。

替代旧版 agent/ 目录（Manager/Strategy/Provider/exceptions 共 190 行）。
本质就是一次 chat.completions.create，加 3 级 JSON 解析兜底。
"""
import json
import hashlib
import logging
import re
from typing import Optional
from urllib.parse import urlparse

from openai import OpenAI

from config import LLM_API_KEY, LLM_BASE_URL, LL_MODEL


logger = logging.getLogger(__name__)


def normalize_runtime_config(value: dict | None) -> dict:
    """Validate a request-scoped OpenAI-compatible model configuration."""
    if not isinstance(value, dict):
        raise ValueError("AI 模型配置格式无效。")

    provider = str(value.get("provider") or "custom").strip().lower()
    if provider not in {"deepseek", "custom"}:
        raise ValueError("AI 服务商仅支持 DeepSeek 或其他兼容接口。")

    api_key = str(value.get("api_key") or value.get("apiKey") or "").strip()
    base_url = str(value.get("base_url") or value.get("baseUrl") or "").strip()
    model = str(value.get("model") or "").strip()
    if provider == "deepseek":
        base_url = base_url or "https://api.deepseek.com"
        model = model or "deepseek-v4-flash"

    if not api_key:
        raise ValueError("请填写 API Key。")
    if len(api_key) > 4096:
        raise ValueError("API Key 长度超出限制。")
    if not base_url or len(base_url) > 500:
        raise ValueError("请填写有效的接口地址。")

    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("接口地址必须是有效的 HTTP 或 HTTPS 地址。")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("接口地址不能包含账号、密码、查询参数或锚点。")
    if not model or len(model) > 160 or any(char in model for char in "\r\n\t"):
        raise ValueError("请填写有效的模型名称。")

    return {
        "provider": provider,
        "api_key": api_key,
        "base_url": base_url.rstrip("/"),
        "model": model,
    }


def model_config_fingerprint(value: dict | None) -> str:
    """Build a cache discriminator without exposing request credentials."""
    if value is None:
        material = f"server-default\0{LLM_BASE_URL}\0{LL_MODEL}"
    else:
        config = normalize_runtime_config(value)
        material = "\0".join(
            (
                config["provider"],
                config["base_url"],
                config["model"],
                config["api_key"],
            )
        )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


def _runtime_client(value: dict) -> tuple[OpenAI, dict]:
    config = normalize_runtime_config(value)
    client = OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        timeout=300.0,
    )
    return client, config


def _get_client() -> OpenAI:
    if not LLM_API_KEY:
        raise RuntimeError(
            "LLM_API_KEY 未配置，请在 apps/backend/.env 填写后重启后端。"
        )
    return OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL, timeout=300.0)


# 模块级单例客户端，避免每次请求重建
_client: Optional[OpenAI] = None


def _client_singleton() -> OpenAI:
    global _client
    if _client is None:
        _client = _get_client()
    return _client


def _message_texts(message) -> list[str]:
    """Collect text candidates from content and reasoning response shapes."""
    candidates: list[str] = []
    content = message.get("content") if isinstance(message, dict) else getattr(message, "content", None)
    if isinstance(content, dict):
        nested = content.get("text") or content.get("content") or content.get("value")
        if isinstance(nested, str) and nested.strip():
            candidates.append(nested.strip())
    elif isinstance(content, str) and content.strip():
        candidates.append(content.strip())
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
            elif isinstance(getattr(item, "text", None), str):
                parts.append(item.text)
        joined = "".join(parts).strip()
        if joined:
            candidates.append(joined)

    # Reasoning models may put the JSON in reasoning_content, either alongside
    # content or instead of it.
    reasoning = message.get("reasoning_content") if isinstance(message, dict) else getattr(message, "reasoning_content", None)
    if isinstance(reasoning, str) and reasoning.strip():
        candidates.append(reasoning.strip())
    return candidates


def _message_text(message) -> str:
    """Read the first non-empty text from an OpenAI-compatible response."""
    candidates = _message_texts(message)
    return candidates[0] if candidates else ""


def _create_completion(client: OpenAI, request_args: dict):
    """Call compatible gateways that may not support optional JSON controls."""
    try:
        return client.chat.completions.create(**request_args)
    except Exception as exc:
        error_text = str(exc).lower()
        unsupported = []
        if "response_format" in request_args and any(
            token in error_text for token in ("response_format", "json_schema", "structured output")
        ):
            unsupported.append("response_format")
        if any(
            key in request_args and key in error_text
            for key in ("temperature", "top_p")
        ) and any(
            token in error_text
            for token in ("unsupported", "not support", "not allowed", "invalid parameter")
        ):
            unsupported.extend(
                key for key in ("temperature", "top_p") if key in request_args
            )
        if not unsupported:
            raise
        fallback_args = dict(request_args)
        for key in unsupported:
            fallback_args.pop(key, None)
        return client.chat.completions.create(**fallback_args)


def _parse_message_json(message) -> dict:
    """Accept JSON from either visible content or reasoning_content."""
    first_error: ValueError | None = None
    for candidate in _message_texts(message):
        try:
            return parse_json_lenient(candidate)
        except ValueError as exc:
            first_error = first_error or exc
    raise first_error or ValueError("LLM 返回为空，无法解析 JSON")


def _json_retry_prompt(prompt: str) -> str:
    return (
        prompt
        + "\n\n再次强调：只输出一个完整、可解析的 JSON 对象。"
        "不要输出思考过程、分析说明、Markdown、代码围栏或任何 JSON 之外的文字。"
        "所有数组最多 4 条，每条不超过 70 字；所有对象字段必须保留，字段值尽量简洁。"
        "必须以 { 开始并以 } 结束，不能截断。"
    )


def _compact_json_retry_prompt(prompt: str) -> str:
    return (
        prompt
        + "\n\n这是第二次结构化输出尝试。请严格只返回 JSON，不要解释。"
        "为了确保完整返回：每个数组最多 3 条，每条不超过 60 字；"
        "字符串只保留结论和简短证据；所有字段必须存在，未知字段填‘未提供’。"
        "必须从 { 开始，以 } 结束。"
    )


def _json_repair_prompt(prompt: str, response) -> str:
    choices = getattr(response, "choices", None) or []
    message = getattr(choices[0], "message", None) if choices else None
    malformed = "\n\n".join(_message_texts(message))[-12000:]
    return (
        prompt
        + "\n\n前一次 AI 输出未通过 JSON 语法校验。请根据原始简历和岗位要求，"
        "修复或重新生成一个完整 JSON 对象。只能输出 JSON；所有字段必须存在；"
        "每个数组最多 3 条，每条不超过 60 字，未知信息填‘未提供’。"
        + (f"\n\n待修复的 AI 输出：\n{malformed}" if malformed else "")
    )


def _response_debug(response) -> str:
    """Return bounded response metadata for diagnosing malformed JSON."""
    choices = getattr(response, "choices", None) or []
    if not choices:
        return "finish_reason=None; candidate_lengths=[]; preview=''"
    choice = choices[0]
    finish_reason = getattr(choice, "finish_reason", None)
    candidates = _message_texts(getattr(choice, "message", None))
    lengths = [len(candidate) for candidate in candidates]
    preview = " | ".join(candidate[:240] for candidate in candidates)
    return f"finish_reason={finish_reason!r}; candidate_lengths={lengths}; preview={preview!r}"


def call_llm(
    prompt: str,
    expect_json: bool = False,
    max_tokens: int | None = None,
    thinking: bool = False,
    runtime_config: dict | None = None,
):
    """Call the configured LLM and parse structured results when requested."""
    if runtime_config is None:
        client = _client_singleton()
        model = LL_MODEL
        base_url = LLM_BASE_URL
    else:
        client, config = _runtime_client(runtime_config)
        model = config["model"]
        base_url = config["base_url"]
    request_args = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "top_p": 0.9,
        "timeout": 300,
    }
    if max_tokens is not None:
        request_args["max_tokens"] = max_tokens
    if expect_json:
        request_args["response_format"] = {"type": "json_object"}
    if "api.deepseek.com" in base_url and model.startswith("deepseek-v4"):
        request_args["extra_body"] = {
            "thinking": {"type": "enabled" if thinking else "disabled"}
        }

    response = _create_completion(client, request_args)
    if not expect_json:
        return _message_text(response.choices[0].message)

    try:
        return _parse_message_json(response.choices[0].message)
    except ValueError as first_error:
        logger.warning("LLM JSON parse failed; %s; error=%s", _response_debug(response), first_error)
        retry_args = dict(request_args)
        retry_args["max_tokens"] = max(int(max_tokens or 0), 7000)
        retry_args["messages"] = [
            {
                "role": "user",
                "content": _compact_json_retry_prompt(_json_retry_prompt(prompt)),
            }
        ]
        try:
            retry_response = _create_completion(client, retry_args)
            return _parse_message_json(retry_response.choices[0].message)
        except ValueError as retry_error:
            logger.warning("LLM JSON retry failed; %s; error=%s", _response_debug(retry_response), retry_error)
            repair_args = dict(retry_args)
            repair_args["messages"] = [
                {
                    "role": "user",
                    "content": _json_repair_prompt(prompt, retry_response),
                }
            ]
            repair_response = _create_completion(client, repair_args)
            try:
                return _parse_message_json(repair_response.choices[0].message)
            except ValueError as repair_error:
                logger.warning(
                    "LLM JSON repair failed; %s; error=%s",
                    _response_debug(repair_response),
                    repair_error,
                )
                raise ValueError(
                    f"首次解析失败：{first_error}；重试解析失败：{retry_error}；"
                    f"AI 修复解析失败：{repair_error}"
                ) from repair_error


# ── JSON 解析兜底（照搬旧版 JSONWrapper 的 3 级策略）──────────────────

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*([\s\S]+?)```", re.IGNORECASE)


def _find_balanced_json(text: str) -> str | None:
    """
    从第一个 { 开始，按字符串字面量/转义 跳过后配对找对应 }。
    避免正则贪婪把多个对象拼成一个（这是大模型返回里很常见的 bug）。
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        c = text[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def parse_json_lenient(text: str) -> dict:
    """
    容错解析 LLM 返回的 JSON 文本。3 级兜底：
      1. 直接 json.loads
      2. 抽 ```json ... ``` 代码块再解析
      3. 平衡括号抽第一个 {...} 再解析（替代原先的贪婪正则）
    全部失败抛 ValueError。
    """
    if not text:
        raise ValueError("LLM 返回为空，无法解析 JSON")

    text = text.strip()

    # 1. 直接解析
    try:
        return json.loads(text, strict=False)
    except (json.JSONDecodeError, TypeError):
        pass

    # 2. 抽 ```json``` 代码块
    m = _JSON_FENCE_RE.search(text)
    if m:
        try:
            return json.loads(m.group(1).strip(), strict=False)
        except (json.JSONDecodeError, TypeError):
            pass

    # 3. 平衡括号抽 {...}
    candidate = _find_balanced_json(text)
    if candidate:
        try:
            return json.loads(candidate, strict=False)
        except (json.JSONDecodeError, TypeError):
            pass

    # A trailing comma is a frequent model formatting defect. Removing only a
    # comma immediately before a closing bracket does not alter analysis data.
    without_trailing_commas = re.sub(r",\s*([}\]])", r"\1", candidate or text)
    try:
        return json.loads(without_trailing_commas, strict=False)
    except (json.JSONDecodeError, TypeError):
        pass

    raise ValueError(f"LLM 返回无法解析为 JSON，前 200 字: {text[:200]}")
