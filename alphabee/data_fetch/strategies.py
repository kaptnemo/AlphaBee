"""Fix strategy recommendation — maps error patterns to actionable fix plans.

Each strategy includes:
- *fix_strategy*: one of switch_source / add_field / fix_interface / fix_crawler / fallback
- *recommended_actions*: concrete steps the agent should take
- *relevant_paths*: files the agent should read and potentially modify
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alphabee.data_fetch.models import ErrorType, FixStrategy


@dataclass
class FixPlan:
    strategy: FixStrategy
    recommended_actions: list[str] = field(default_factory=list)
    relevant_paths: list[str] = field(default_factory=list)
    agent_instruction: str = ""


# ── provider → known code paths ────────────────────────────────────────

_PROVIDER_PATHS: dict[str, list[str]] = {
    "tushare": [
        "alphabee/collectors/tushare/helper.py",
        "alphabee/adapters/tushare.py",
        "alphabee/adapters/tushare/*.yaml",
    ],
    "akshare": [
        "alphabee/collectors/akshare/helper.py",
        "alphabee/adapters/akshare/*.yaml",
    ],
    "eastmoney": [
        "alphabee/collectors/eastmoney/helper.py",
    ],
    "baostock": [
        "alphabee/collectors/baostock/helper.py",
    ],
    "local": [
        "alphabee/collectors/local/helper.py",
    ],
    "crawler": [
        "alphabee/collectors/*/",
    ],
}

# ── error-type → fix plan ──────────────────────────────────────────────


def recommend_fix(
    provider: str,
    api_name: str,
    error_type: str,
    error_message: str | None = None,
) -> FixPlan:
    """Return a recommended fix plan for a given failure pattern."""

    provider_paths = _PROVIDER_PATHS.get(provider.lower(), [])
    et = _normalise_et(error_type)

    if et == ErrorType.PERMISSION:
        return FixPlan(
            strategy=FixStrategy.FIX_INTERFACE,
            recommended_actions=[
                f"检查 {provider.upper()} token 是否过期",
                f"确认 {provider.upper()} 接口 '{api_name}' 是否需要更高权限等级",
                "若 token 正常，检查接口是否已被官方废弃或变更",
            ],
            relevant_paths=[
                *provider_paths,
                "alphabee/collectors/tushare/__init__.py",
                "config.yaml",
            ],
            agent_instruction="读取 config.yaml 确认 API token 配置，检查接口文档确认权限要求。",
        )

    if et == ErrorType.MISSING_FIELD:
        return FixPlan(
            strategy=FixStrategy.ADD_FIELD,
            recommended_actions=[
                f"检查 '{api_name}' 接口返回的字段列表",
                "在对应的 adapter YAML 中补充字段映射",
                "若字段已废弃，从 schema INDEX.yaml 中移除或标记为 deprecated",
            ],
            relevant_paths=[
                f"alphabee/adapters/{provider.lower()}/*.yaml",
                "alphabee/schemas/INDEX.yaml",
                *provider_paths,
            ],
            agent_instruction="读取 adapter YAML 和接口返回数据，补全缺失字段的映射关系。",
        )

    if et == ErrorType.TIMEOUT:
        return FixPlan(
            strategy=FixStrategy.SWITCH_SOURCE,
            recommended_actions=[
                f"检查 {provider.upper()} 服务是否可用（手动 curl / ping）",
                "增加重试次数和指数退避时间",
                "添加备用数据源（AkShare / Baostock / Eastmoney）作为 fallback",
                "对频繁超时的接口添加熔断机制",
            ],
            relevant_paths=[
                *provider_paths,
                *_alternate_sources(provider),
            ],
            agent_instruction="先确认上游服务健康状态，然后增加重试逻辑或切换备用数据源。",
        )

    if et == ErrorType.NETWORK:
        return FixPlan(
            strategy=FixStrategy.FALLBACK,
            recommended_actions=[
                "检查代理配置 (config.yaml proxy_url)",
                "添加 DNS 缓存或重试逻辑",
                "切换到不需要代理的备用数据源",
            ],
            relevant_paths=[
                *provider_paths,
                "config.yaml",
            ],
            agent_instruction="检查网络连接和代理配置，添加备用数据源作为 fallback。",
        )

    if et == ErrorType.RATE_LIMIT:
        return FixPlan(
            strategy=FixStrategy.FALLBACK,
            recommended_actions=[
                "在 wrap 层添加速率限制（令牌桶 / 漏桶）",
                "增加重试延迟时间",
                f"对高频接口 '{api_name}' 添加本地缓存以减少调用次数",
            ],
            relevant_paths=[
                *provider_paths,
                "alphabee/tools/cache.py",
            ],
            agent_instruction="添加速率限制或本地缓存以降低对上游接口的调用频率。",
        )

    if et == ErrorType.PARSE_ERROR:
        return FixPlan(
            strategy=FixStrategy.FIX_INTERFACE,
            recommended_actions=[
                f"检查 '{api_name}' 返回的数据格式是否发生变化",
                "更新 adapter 的列名映射或数据类型转换逻辑",
                "添加 defensive parsing (safe_float / safe_str)",
            ],
            relevant_paths=[
                *provider_paths,
                f"alphabee/adapters/{provider.lower()}/",
                "alphabee/agents/facts/tools/_utils.py",
            ],
            agent_instruction="读取接口返回的原始数据，修复解析逻辑或 adapter 映射。",
        )

    if et == ErrorType.EMPTY_RESPONSE:
        return FixPlan(
            strategy=FixStrategy.SWITCH_SOURCE,
            recommended_actions=[
                f"确认 '{api_name}' 接口在给定参数下是否确实无数据",
                "检查参数格式是否正确（日期格式、stock code 格式）",
                "添加备用数据源 fallback",
            ],
            relevant_paths=[
                *provider_paths,
                *_alternate_sources(provider),
                "alphabee/tools/common.py",
            ],
            agent_instruction="先验证参数格式，若接口不返回任何数据则切换到备用数据源。",
        )

    # unknown / uncategorised
    return FixPlan(
        strategy=FixStrategy.FIX_INTERFACE,
        recommended_actions=[
            f"排查 {provider}.{api_name} 的异常原因",
            f"原始错误: {error_message or 'N/A'}",
            "检查接口是否变更、网络是否正常、参数是否正确",
        ],
        relevant_paths=[*provider_paths],
        agent_instruction="读取错误详情和相关代码，诊断根因后修复。",
    )


# ── helpers ────────────────────────────────────────────────────────────


def _normalise_et(error_type: str) -> ErrorType | None:
    try:
        return ErrorType(error_type)
    except ValueError:
        return None


def _alternate_sources(provider: str) -> list[str]:
    """Return paths for alternate data sources."""
    alternates: dict[str, list[str]] = {
        "tushare": [
            "alphabee/collectors/akshare/helper.py",
            "alphabee/collectors/baostock/helper.py",
        ],
        "akshare": [
            "alphabee/collectors/tushare/helper.py",
            "alphabee/collectors/baostock/helper.py",
        ],
        "eastmoney": [
            "alphabee/collectors/akshare/helper.py",
        ],
        "baostock": [
            "alphabee/collectors/tushare/helper.py",
            "alphabee/collectors/akshare/helper.py",
        ],
    }
    return alternates.get(provider.lower(), [])
