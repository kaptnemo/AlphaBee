from __future__ import annotations

import time
from collections.abc import Sequence
from threading import Lock
from typing import Any, cast
from uuid import UUID

import structlog
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.messages import BaseMessage
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion, ChatCompletionMessageParam
from pydantic import SecretStr

from alphabee.config import settings

logger = structlog.get_logger(__name__)


def _require_llm_api_key() -> str:
    raw_api_key = settings.llm.api_key
    api_key = raw_api_key.strip()
    normalized = api_key.lower()
    if not api_key or normalized in {"none", "null"} or api_key.startswith("${"):
        raise OSError("LLM API key is not configured. Set LLM_API_KEY or provide llm.api_key in config.yaml.")
    return api_key


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _extract_nested_int(data: dict[str, Any], *paths: tuple[str, ...]) -> int | None:
    for path in paths:
        current: Any = data
        for key in path:
            if not isinstance(current, dict):
                current = None
                break
            current = current.get(key)
        coerced = _coerce_int(current)
        if coerced is not None:
            return coerced
    return None


def _normalize_usage(raw_usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = raw_usage or {}
    prompt_tokens = _extract_nested_int(
        usage,
        ("prompt_tokens",),
        ("input_tokens",),
    )
    completion_tokens = _extract_nested_int(
        usage,
        ("completion_tokens",),
        ("output_tokens",),
    )
    total_tokens = _extract_nested_int(
        usage,
        ("total_tokens",),
    )
    if total_tokens is None and prompt_tokens is not None and completion_tokens is not None:
        total_tokens = prompt_tokens + completion_tokens

    prompt_cached_tokens = _extract_nested_int(
        usage,
        ("prompt_tokens_details", "cached_tokens"),
        ("input_token_details", "cache_read"),
        ("input_token_details", "cached_tokens"),
    )
    completion_reasoning_tokens = _extract_nested_int(
        usage,
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_token_details", "reasoning"),
        ("output_token_details", "reasoning_tokens"),
    )

    return {
        "prompt_tokens": prompt_tokens or 0,
        "completion_tokens": completion_tokens or 0,
        "total_tokens": total_tokens or 0,
        "prompt_cached_tokens": prompt_cached_tokens or 0,
        "completion_reasoning_tokens": completion_reasoning_tokens or 0,
    }


def _extract_usage_from_langchain_result(response: LLMResult) -> dict[str, Any]:
    llm_output = response.llm_output or {}
    raw_usage = None
    model_name = llm_output.get("model_name") or llm_output.get("model")
    request_id = llm_output.get("id") or llm_output.get("request_id")

    if isinstance(llm_output.get("token_usage"), dict):
        raw_usage = llm_output["token_usage"]
    elif isinstance(llm_output.get("usage"), dict):
        raw_usage = llm_output["usage"]

    if response.generations and response.generations[0]:
        generation = response.generations[0][0]
        message = getattr(generation, "message", None)
        usage_metadata = getattr(message, "usage_metadata", None)
        if raw_usage is None and isinstance(usage_metadata, dict):
            raw_usage = usage_metadata

        response_metadata = getattr(message, "response_metadata", None)
        if isinstance(response_metadata, dict):
            model_name = model_name or response_metadata.get("model_name") or response_metadata.get("model")
            request_id = request_id or response_metadata.get("id") or response_metadata.get("request_id")

    return {
        "model": model_name or settings.llm.model,
        "request_id": request_id,
        **_normalize_usage(raw_usage),
    }


def _extract_usage_from_openai_response(response: ChatCompletion) -> dict[str, Any]:
    raw_usage: dict[str, Any] | None = None
    if response.usage is not None:
        if hasattr(response.usage, "model_dump"):
            raw_usage = response.usage.model_dump()
        elif isinstance(response.usage, dict):
            raw_usage = response.usage

    return {
        "model": response.model or settings.llm.model,
        "request_id": response.id,
        **_normalize_usage(raw_usage),
    }


class TokenUsageCallbackHandler(BaseCallbackHandler):
    def __init__(self, component: str) -> None:
        self.component = component
        self._started_at: dict[UUID, float] = {}
        self._lock = Lock()

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            self._started_at[run_id] = time.monotonic()

    def on_llm_end(
        self,
        response: LLMResult,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            started_at = self._started_at.pop(run_id, None)
        usage = _extract_usage_from_langchain_result(response)
        latency_ms = round((time.monotonic() - started_at) * 1000, 2) if started_at is not None else None
        logger.info(
            "llm.usage",
            component=self.component,
            provider="langchain_openai",
            model=usage["model"],
            request_id=usage["request_id"],
            prompt_tokens=usage["prompt_tokens"],
            completion_tokens=usage["completion_tokens"],
            total_tokens=usage["total_tokens"],
            prompt_cached_tokens=usage["prompt_cached_tokens"],
            completion_reasoning_tokens=usage["completion_reasoning_tokens"],
            latency_ms=latency_ms,
            tags=tags or [],
        )

    def on_llm_error(
        self,
        error: BaseException,
        *,
        run_id: UUID,
        parent_run_id: UUID | None = None,
        tags: list[str] | None = None,
        **kwargs: Any,
    ) -> Any:
        with self._lock:
            started_at = self._started_at.pop(run_id, None)
        latency_ms = round((time.monotonic() - started_at) * 1000, 2) if started_at is not None else None
        logger.error(
            "llm.error",
            component=self.component,
            provider="langchain_openai",
            latency_ms=latency_ms,
            tags=tags or [],
            error=str(error),
            exc_info=(type(error), error, error.__traceback__),
        )


def create_chat_model(component: str, **kwargs: Any) -> ChatOpenAI:
    callbacks = list(kwargs.pop("callbacks", []) or [])
    callbacks.append(TokenUsageCallbackHandler(component))

    tags = list(kwargs.pop("tags", []) or [])
    tags.append(f"component:{component}")

    return ChatOpenAI(
        model=settings.llm.model,
        api_key=SecretStr(_require_llm_api_key()),
        base_url=settings.llm.base_url,
        callbacks=callbacks,
        tags=tags,
        **kwargs,
    )


# ── Structured output support ───────────────────────────────────────────────
#
# 端点能力（2026-08 冒烟验证，deepseek-v4-pro @ api.deepseek.com）：
#   - response_format={"type": "json_object"}      ✅ 可用（要求 prompt 含 "json" 字样）
#   - response_format={"type": "json_schema"}      ❌ "This response_format type is unavailable"
#   - 强制 tool_choice 的 function calling 结构化输出 ❌ "Thinking mode does not support this tool_choice"
#
# 因此本项目不使用 with_structured_output(json_schema / function_calling)，
# 只绑定 json_object **容器约束**：API 层保证输出是合法 JSON 对象
# （无散文前缀/后缀、无 Markdown 栅栏），字段级结构仍由 json_instruction +
# parse_json + Pydantic 校验 + 既有降级链保证。deepagents 站点
# （explore_conflicts / verify_hypotheses / synthesize_insights）暂不迁移：
# create_deep_agent 的 response_format 会走 ProviderStrategy/ToolStrategy，
# 在本端点均不可用，待端点支持 json_schema 后再迁移。

JSON_OBJECT_RESPONSE_FORMAT: dict[str, str] = {"type": "json_object"}


def create_structured_model(component: str, **kwargs: Any) -> Any:
    """``create_chat_model`` + bind(response_format=json_object) 的直连结构化模型。

    只保证 JSON **容器**，不保证 schema 级字段约束——调用方必须保留
    ``parse_json`` + Pydantic 校验与既有降级/重试链。prompt 必须包含
    "json"（任意大小写）字样，否则端点返回 400。
    当 ``settings.llm.structured_json`` 为 False 时退化为普通模型（旧行为）。
    """
    model = create_chat_model(component, **kwargs)
    if settings.llm.structured_json:
        return model.bind(response_format=dict(JSON_OBJECT_RESPONSE_FORMAT))
    return model


def create_async_openai_client() -> AsyncOpenAI:
    return AsyncOpenAI(
        api_key=_require_llm_api_key(),
        base_url=settings.llm.base_url,
    )


async def tracked_chat_completion(
    *,
    component: str,
    messages: Sequence[ChatCompletionMessageParam],
    model: str | None = None,
    json_mode: bool = False,
    **kwargs: Any,
) -> ChatCompletion:
    """Tracked raw chat completion（带用量日志）。

    Args:
        json_mode: True 且 ``settings.llm.structured_json`` 开启时注入
            ``response_format=json_object``（要求 messages 含 "json" 字样）。
            解析/校验仍由调用方负责。
    """
    started_at = time.monotonic()
    request_model = model or settings.llm.model
    client = create_async_openai_client()
    if json_mode and settings.llm.structured_json:
        kwargs.setdefault("response_format", dict(JSON_OBJECT_RESPONSE_FORMAT))
    try:
        response = await client.chat.completions.create(
            model=request_model,
            messages=list(messages),
            **kwargs,
        )
    except Exception:
        logger.exception(
            "llm.error",
            component=component,
            provider="openai",
            model=request_model,
            latency_ms=round((time.monotonic() - started_at) * 1000, 2),
        )
        raise

    usage = _extract_usage_from_openai_response(response)
    logger.info(
        "llm.usage",
        component=component,
        provider="openai",
        model=usage["model"],
        request_id=usage["request_id"],
        prompt_tokens=usage["prompt_tokens"],
        completion_tokens=usage["completion_tokens"],
        total_tokens=usage["total_tokens"],
        prompt_cached_tokens=usage["prompt_cached_tokens"],
        completion_reasoning_tokens=usage["completion_reasoning_tokens"],
        latency_ms=round((time.monotonic() - started_at) * 1000, 2),
    )
    return cast(ChatCompletion, response)
