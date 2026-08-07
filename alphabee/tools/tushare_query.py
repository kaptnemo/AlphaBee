"""Dynamic Tushare query tool — lets agents call any Tushare API by name.

上下文优化设计（避免大表原始数据进入 LLM 上下文）：
- **服务端全量抓取一次 + 渲染时投影**：底层 API 只按业务参数（剥离 ``fields``/``preview``）
  抓取一次完整结果，结果按 ``(api_name, 归一化后的抓取参数)`` 缓存；不同 ``fields``
  的渲染共享同一份抓取，互不重复请求。
- **``fields`` 只渲染指定列**：模型在调用时用 ``fields`` 声明所需列，列裁剪发生在渲染层，
  全量列矩阵永远不会进入上下文。字段名同时兼容 Tushare 源字段名与 AlphaBee canonical 名。
- **``preview`` 便宜预览**：模型不确定该接口有哪些可用列时，先 ``preview=True`` 拿到
  列名清单 + AlphaBee 关键列推荐，再据此传 ``fields`` 二次调用（命中缓存，零额外抓取）。
- **默认白名单兜底**：未传 ``fields`` 时，按适配器字段映射（``adapters/tushare/*.yaml``）
  中的该接口关键列投影，避免全量列进入上下文。
- **数值归一**：NaN/None 显示为 ``-``，长尾浮点与超大量级数值压缩为紧凑形式，
  进一步降低上下文 token 占用。
"""

import json

import pandas as pd

from alphabee.adapters.tushare import TuShare_Adapter
from alphabee.collectors.tushare.helper import TuShareHelper
from alphabee.tools.cache import SyncTTLCache

_QUERY_CACHE = SyncTTLCache(ttl_seconds=300.0)

# 渲染层控制项：不参与 Tushare 接口调用，抓取前剥离
_RENDER_CONTROL_PARAMS = ("fields", "preview")


def _fetch_params(params_dict: dict) -> dict:
    """剥离渲染层控制项，返回真正传给 Tushare 接口的抓取参数。"""
    return {k: v for k, v in params_dict.items() if k not in _RENDER_CONTROL_PARAMS}


def _parse_fields(fields: str) -> list[str]:
    """把逗号分隔的 fields 字符串解析为列名列表。"""
    if not fields or not fields.strip():
        return []
    return [f.strip() for f in fields.split(",") if f.strip()]


def _default_fields(api_name: str) -> list[str] | None:
    """从适配器字段映射构建该接口的关键列白名单（canonical 列名）。

    适配器 YAML（``adapters/tushare/*.yaml``）以 Tushare 源字段为 key、canonical
    字段为 value，是 AlphaBee 治理过的"业务关心字段"。helper 在返回前已把源字段
    重命名为 canonical 名，因此这里取 mapping 的 value 作为默认投影列。
    """
    mapping = TuShare_Adapter.adapter_config.get(api_name)
    if not mapping or not isinstance(mapping, dict):
        return None
    values = [v for v in mapping.values() if isinstance(v, str)]
    return values or None


def _resolve_requested_fields(api_name: str, requested: list[str]) -> list[str]:
    """把 Tushare 源字段名翻译为适配后的 canonical 列名（容忍两种写法）。

    模型可能照着 Tushare 文档写 ``accounts_receiv``，也可能照着 preview 输出写
    ``accounts_receivable``。这里用适配器映射把源字段名换算成实际 df 中的列名，
    再统一做存在性校验。
    """
    mapping = TuShare_Adapter.adapter_config.get(api_name)
    if not mapping or not isinstance(mapping, dict):
        return requested
    return [mapping.get(f, f) for f in requested]


def _format_value(value) -> str:
    """数值归一：None/NaN→'-'，长尾浮点与超大量级数值压缩为紧凑形式。"""
    if value is None:
        return "-"
    try:
        if pd.isna(value):
            return "-"
    except (TypeError, ValueError):
        pass
    if isinstance(value, float):
        abs_val = abs(value)
        # 量级很大（如 3.4e9 元）或很小（如 0.0001）的浮点用 3 位有效数字，
        # 兼顾紧凑与可读；常规数值保留两位小数并去掉尾零。
        if abs_val >= 1e7 or (abs_val > 0 and abs_val < 1e-3):
            return f"{value:.3g}"
        return f"{value:.2f}".rstrip("0").rstrip(".")
    return str(value)


def _render_markdown(df: pd.DataFrame) -> str:
    """逐格数值归一后渲染为 Markdown 表格。"""
    return df.map(_format_value).to_markdown(index=False)


def _render_preview(api_name: str, normalized_params: str, df: pd.DataFrame) -> str:
    """preview 模式：只返回列名清单 + AlphaBee 关键列推荐，不渲染任何数据行。"""
    columns = list(df.columns)
    lines = [
        f"**接口**: `{api_name}` | **共 {len(columns)} 列**（preview 模式，仅列名，未渲染数据）",
        f"**参数**: `{normalized_params}`",
        "",
        f"**可用列**（共 {len(columns)} 列）:",
        "- " + ", ".join(columns),
    ]
    default = _default_fields(api_name)
    if default:
        keep = [c for c in default if c in columns]
        if keep:
            lines += [
                "",
                "**AlphaBee 关键列**（字段映射推荐，优先选择）:",
                "- " + ", ".join(keep),
            ]
    lines += [
        "",
        "**使用建议**: 只请求你需要的列，避免全量列进入上下文。",
        f'示例：`query_tushare(api_name="{api_name}", params=\'{normalized_params}\', fields="{columns[0]}")`',
    ]
    return "\n".join(lines)


def query_tushare(api_name: str, params: str, fields: str = "", max_rows: int = 50, preview: bool = False) -> str:
    """动态调用任意 Tushare 接口获取数据，供 agent 根据问题自主选择接口和参数。

    当 agent 需要从 Tushare 获取特定数据时调用，包括但不限于：
    - 行情数据：daily, weekly, monthly, pro_bar, daily_basic
    - 财务数据：income, balancesheet, cashflow, fina_indicator, forecast, express
    - 资金流向：moneyflow, moneyflow_hsgt, hsgt_top10, top_list
    - 板块/指数：index_basic, index_daily, sw_daily, ths_index, ths_member, index_classify
    - 基础信息：stock_basic, stock_company, trade_cal
    - 公告/新闻：anns_d, news, major_news, research_report
    - 宏观数据：cn_cpi, cn_ppi, cn_pmi, cn_gdp, cn_m, sf_month, shibor, shibor_lpr

    股票代码须为 Tushare 标准格式，如 "600519.SH"（沪市）、"300750.SZ"（深市）。

    ⚠️ **上下文优化约束（必须遵守）**：
    1. 每次调用**必须**用 `fields` 只请求当前任务直接需要的列，禁止请求全量列。
    2. 不确定该接口有哪些可用列时，先 `preview=True` 获取列名清单与 AlphaBee 关键列
       推荐，再据此传 `fields` 二次调用（命中缓存，零额外抓取）。
    3. `fields` 字段名兼容 Tushare 源字段名与 AlphaBee canonical 名两种写法。

    Args:
        api_name: Tushare 接口名称，如 'daily'、'income'、'fina_indicator' 等
        params:   JSON 格式的接口参数字符串，例如：
                  '{"ts_code": "600519.SH", "start_date": "20240101", "end_date": "20241231"}'
                  '{"ts_code": "300750.SZ", "start_date": "20230101"}'
                  日期格式统一为 YYYYMMDD，如 '20240101'
        fields:   逗号分隔的列名白名单，例如 'end_date,accounts_receivable,total_assets'。
                  只渲染这些列，未传时按该接口的 AlphaBee 关键列投影。可用 preview=True
                  查看完整列名清单。
        max_rows: 返回数据最大行数，默认 50，最大 200
        preview:  为 True 时只返回列名清单 + 关键列推荐（不渲染数据），用于模型在
                  不确定可用列时先做便宜的 schema 侦察，再传 fields 二次调用。

    Returns:
        Markdown 格式的数据表格，包含接口名、参数摘要和数据内容。
        若接口返回空数据，将说明可能的原因（非交易日、未上市、权限不足等）。
    """
    max_rows = max(1, min(max_rows, 200))

    try:
        params_dict = json.loads(params)
    except json.JSONDecodeError as e:
        return (
            f"❌ 参数解析失败：{e}\n"
            "请确保 params 是合法的 JSON 字符串，"
            '例如：\'{"ts_code": "600519.SH", "start_date": "20240101"}\''
        )

    # 抓取层参数剥离 fields/preview：同一接口参数只抓一次，不同投影共享缓存
    fetch_params = _fetch_params(params_dict)
    normalized_params = json.dumps(fetch_params, sort_keys=True, ensure_ascii=False)
    cache_key = ("query_tushare", api_name, normalized_params)

    def _compute() -> pd.DataFrame | None:
        with TuShareHelper() as helper:
            api_fn = getattr(helper, api_name, None)
            if api_fn is None:
                raise ValueError(f"未找到 Tushare 接口：`{api_name}`")
            result = api_fn(**fetch_params)
            df = result.data
        if df is None or df.empty:
            return None
        return df

    try:
        df = _QUERY_CACHE.get_or_compute(cache_key, _compute)
    except ValueError as exc:
        return f"❌ {exc}"
    except Exception as exc:
        return (
            f"❌ 接口 `{api_name}` 调用失败：{exc}\n"
            f"参数：`{normalized_params}`\n"
            "可能原因：非交易日、标的未上市、参数错误或积分/权限不足。"
        )

    if df is None:
        return (
            f"接口 `{api_name}` 返回空数据\n"
            f"参数：`{normalized_params}`\n"
            "可能原因：非交易日、标的未上市、参数错误或积分/权限不足。"
        )

    columns = list(df.columns)

    # ── preview 模式：只给列名清单 + 关键列推荐 ──────────────────────────
    if preview:
        return _render_preview(api_name, normalized_params, df)

    # ── 投影列选择 ───────────────────────────────────────────────────────
    requested = _parse_fields(fields)
    note = ""
    if requested:
        resolved = _resolve_requested_fields(api_name, requested)
        selected = [c for c in resolved if c in columns]
        unknown = [c for c in resolved if c not in columns]
        if unknown:
            note = f"\n\n> ⚠️ 已忽略不存在的字段：{', '.join(unknown)}（可用 preview=True 查看全部列名）"
        if not selected:
            return (
                f"❌ 接口 `{api_name}` 中不存在请求的字段：{', '.join(requested)}。\n"
                f"可用列：{', '.join(columns[:20])}{'…' if len(columns) > 20 else ''}"
            )
    else:
        default = _default_fields(api_name)
        if default:
            selected = [c for c in default if c in columns]
            note = (
                f"\n\n> 未指定 fields，已按 AlphaBee 关键列投影"
                f"（省略 {len(columns) - len(selected)} 列）；可用 preview=True 查看完整列名。"
            )
        else:
            selected = columns
            note = f"\n\n> ⚠️ 接口 `{api_name}` 无字段映射白名单，返回全部 {len(columns)} 列。"

    total_rows = len(df)
    df_display = df[selected].head(max_rows)

    lines = [
        f"**接口**: `{api_name}` | **共 {total_rows} 行**（显示前 {len(df_display)} 行）",
        f"**参数**: `{normalized_params}`",
        "",
        _render_markdown(df_display),
    ]
    if total_rows > max_rows:
        lines.append(f"\n> 共 {total_rows} 行，只显示前 {max_rows} 行。如需更多数据，请缩小时间范围或增大 max_rows。")
    if note:
        lines.append(note)

    return "\n".join(lines)
