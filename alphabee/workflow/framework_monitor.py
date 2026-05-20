from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, Field

from alphabee.config import settings
from alphabee.tools.common import web_search
from alphabee.tools.fundamentals import get_fundamentals
from alphabee.tools.market_data import get_market_data
from alphabee.tools.news import get_stock_news_summary


class MonitorStage(BaseModel):
    name: str = Field(description="阶段判断，例如止跌企稳、边际改善、趋势反转")
    confidence: Literal["低", "中", "高"] = Field(description="当前判断置信度")
    reason: str = Field(description="阶段判断依据")


class MonitorAlert(BaseModel):
    title: str = Field(description="提示标题")
    severity: Literal["high", "medium", "low"] = Field(description="提示级别")
    status: Literal["new", "ongoing", "resolved", "info"] = Field(description="相对上次快照的状态")
    reason: str = Field(description="触发提示的原因")
    evidence: list[str] = Field(default_factory=list, description="支撑证据")


class MetricChange(BaseModel):
    metric: str = Field(description="指标名称")
    current_value: str = Field(description="当前值")
    previous_value: str | None = Field(default=None, description="上次值")
    change_summary: str = Field(description="变化摘要")
    importance: Literal["high", "medium", "low"] = Field(description="变化重要性")


class UnavailableCheck(BaseModel):
    check: str = Field(description="无法完成的观察项")
    reason: str = Field(description="缺失原因")


class FrameworkMonitorReport(BaseModel):
    framework_name: str = Field(description="观察框架名称")
    symbol: str = Field(description="股票代码")
    company_name: str = Field(description="公司名称")
    generated_at: str = Field(description="生成时间")
    overall_status: Literal["positive", "neutral", "warning", "critical"] = Field(description="总体状态")
    stage: MonitorStage = Field(description="当前所处阶段判断")
    summary: str = Field(description="本次监控摘要")
    alerts: list[MonitorAlert] = Field(default_factory=list, description="本次监控提示")
    metric_changes: list[MetricChange] = Field(default_factory=list, description="相对上次的关键变化")
    unavailable_checks: list[UnavailableCheck] = Field(default_factory=list, description="暂时无法验证的观察项")
    next_prompt: str = Field(description="适合发给用户的一句提醒")


class MonitorExecutionResult(BaseModel):
    report: FrameworkMonitorReport
    snapshot_path: str
    report_path: str


def _strip_json_fence(raw: str) -> str:
    text = raw.strip()
    if not text.startswith("```"):
        return text
    text = text.split("```", 2)[1]
    if text.startswith("json"):
        text = text[4:]
    return text.rsplit("```", 1)[0].strip()


def _slugify(value: str) -> str:
    normalized = re.sub(r"[^\w\u4e00-\u9fff-]+", "-", value.strip().lower())
    normalized = re.sub(r"-{2,}", "-", normalized).strip("-")
    return normalized or "framework-monitor"


def _digits_only(symbol: str) -> str:
    digits = re.sub(r"\D", "", symbol)
    return digits or symbol


def _trim_news(text: str, max_lines: int = 20) -> str:
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) <= max_lines:
        return "\n".join(lines)
    return "\n".join(lines[:max_lines]) + f"\n...(其余 {len(lines) - max_lines} 条省略)"


def _load_previous_snapshot(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _render_alert_line(alert: MonitorAlert) -> str:
    severity_map = {"high": "高", "medium": "中", "low": "低"}
    status_map = {"new": "新增", "ongoing": "持续", "resolved": "解除", "info": "提示"}
    return f"- [{severity_map[alert.severity]}/{status_map[alert.status]}] **{alert.title}**：{alert.reason}"


def render_monitor_report(result: MonitorExecutionResult) -> str:
    report = result.report
    lines = [
        f"# {report.framework_name} 跟踪报告",
        "",
        f"- 标的：**{report.company_name}（{report.symbol}）**",
        f"- 生成时间：{report.generated_at}",
        f"- 总体状态：**{report.overall_status}**",
        f"- 阶段判断：**{report.stage.name}**（置信度：{report.stage.confidence}）",
        "",
        "## 本次结论",
        report.summary,
        "",
        "## 重点提示",
    ]

    if report.alerts:
        lines.extend(_render_alert_line(alert) for alert in report.alerts)
    else:
        lines.append("- 暂无新增提示。")

    lines.extend(["", "## 关键变化"])
    if report.metric_changes:
        lines.append("| 指标 | 当前值 | 上次值 | 变化 | 重要性 |")
        lines.append("|---|---|---|---|---|")
        for item in report.metric_changes:
            lines.append(
                f"| {item.metric} | {item.current_value} | {item.previous_value or '-'} | "
                f"{item.change_summary} | {item.importance} |"
            )
    else:
        lines.append("- 暂无可确认的关键变化。")

    lines.extend(["", "## 暂缺校验项"])
    if report.unavailable_checks:
        for item in report.unavailable_checks:
            lines.append(f"- **{item.check}**：{item.reason}")
    else:
        lines.append("- 无。")

    lines.extend(
        [
            "",
            "## 建议提示语",
            report.next_prompt,
            "",
            "## 输出文件",
            f"- Markdown 报告：`{result.report_path}`",
            f"- 快照文件：`{result.snapshot_path}`",
        ]
    )
    return "\n".join(lines)


async def _call_monitor_llm(
    framework_name: str,
    framework_markdown: str,
    company_name: str,
    symbol: str,
    fundamentals_payload: dict,
    market_payload: dict,
    news_text: str,
    search_text: str,
    previous_snapshot: dict | None,
) -> FrameworkMonitorReport:
    now = datetime.now().isoformat(timespec="seconds")
    prompt = f"""
你是 AlphaBee 的持续跟踪代理。你的任务不是重新写一篇研报，而是根据“观察框架”判断：
1. 当前有哪些指标/事件发生了变化；
2. 相比上次快照，哪些提示是新增、持续、解除；
3. 哪些观察项仍然无法验证，必须明确说明缺口，严禁编造。

请严格输出 JSON，不要输出 JSON 以外的任何文字。字段必须完整：
{{
  "framework_name": "{framework_name}",
  "symbol": "{symbol}",
  "company_name": "{company_name}",
  "generated_at": "{now}",
  "overall_status": "positive | neutral | warning | critical",
  "stage": {{
    "name": "止跌企稳 | 边际改善 | 趋势反转 | 未确认",
    "confidence": "低 | 中 | 高",
    "reason": "阶段判断依据"
  }},
  "summary": "2-4句摘要，强调边际变化",
  "alerts": [
    {{
      "title": "提示标题",
      "severity": "high | medium | low",
      "status": "new | ongoing | resolved | info",
      "reason": "触发原因",
      "evidence": ["证据1", "证据2"]
    }}
  ],
  "metric_changes": [
    {{
      "metric": "指标名称",
      "current_value": "当前值",
      "previous_value": "上次值或 null",
      "change_summary": "变化说明",
      "importance": "high | medium | low"
    }}
  ],
  "unavailable_checks": [
    {{
      "check": "无法验证的观察项",
      "reason": "原因"
    }}
  ],
  "next_prompt": "一句适合发给用户的提醒"
}}

判断规则：
- 只基于提供的数据与搜索结果，不要补脑。
- 如果某条框架观察项只能部分验证，应放入 unavailable_checks，并在 summary/alerts 中说明局限。
- evidence 必须引用具体数字、日期或检索到的事实。
- 如果上次快照不存在，则 status 只能用 new 或 info。
- overall_status 的含义：
  - positive：多数信号改善，且无重大新增风险
  - neutral：改善和风险并存，未形成确定结论
  - warning：关键指标走弱或关键验证失败
  - critical：出现高确定性的重大负面变化

【观察框架】
{framework_markdown}

【当前基本面数据】
{json.dumps(fundamentals_payload, ensure_ascii=False, indent=2)}

【当前行情数据】
{json.dumps(market_payload, ensure_ascii=False, indent=2)}

【最新相关新闻】
{news_text}

【最新补充搜索】
{search_text}

【上次快照】
{json.dumps(previous_snapshot, ensure_ascii=False, indent=2) if previous_snapshot else "无"}
""".strip()

    client = AsyncOpenAI(
        api_key=settings.llm.api_key,
        base_url=settings.llm.base_url,
    )
    response = await client.chat.completions.create(
        model=settings.llm.model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
    )
    raw = _strip_json_fence(response.choices[0].message.content or "")
    parsed = json.loads(raw)
    return FrameworkMonitorReport.model_validate(parsed)


async def run_framework_monitor(
    framework_path: str,
    symbol: str,
    periods: int = 8,
) -> MonitorExecutionResult:
    framework_file = Path(framework_path)
    if not framework_file.exists():
        raise FileNotFoundError(f"Framework file not found: {framework_path}")

    framework_markdown = framework_file.read_text(encoding="utf-8")
    fundamentals, market_data, news_text = await asyncio.gather(
        get_fundamentals(symbol, periods=max(4, periods)),
        asyncio.to_thread(get_market_data, symbol),
        asyncio.to_thread(get_stock_news_summary, _digits_only(symbol)),
    )

    search_query = (
        f"{fundamentals.name or symbol} 集采 中标 订单 海外 毛利率 最新进展"
    )
    search_text = await web_search(
        query=search_query,
        topic="finance",
        max_results=6,
        days=30,
    )

    project_root = Path(__file__).resolve().parents[2]
    slug = _slugify(framework_file.stem)
    snapshot_path = project_root / "outputs" / "monitor_snapshots" / f"{slug}.json"
    report_path = project_root / "outputs" / "monitor_reports" / f"{slug}.md"
    previous_snapshot = _load_previous_snapshot(snapshot_path)

    report = await _call_monitor_llm(
        framework_name=framework_file.stem,
        framework_markdown=framework_markdown,
        company_name=fundamentals.name,
        symbol=fundamentals.symbol,
        fundamentals_payload=fundamentals.model_dump(),
        market_payload=market_data.model_dump(),
        news_text=_trim_news(news_text),
        search_text=search_text,
        previous_snapshot=previous_snapshot,
    )

    final_report = report.model_copy(
        update={
            "framework_name": framework_file.stem,
            "symbol": fundamentals.symbol,
            "company_name": fundamentals.name,
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        }
    )
    result = MonitorExecutionResult(
        report=final_report,
        snapshot_path=str(snapshot_path.relative_to(project_root)),
        report_path=str(report_path.relative_to(project_root)),
    )

    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    snapshot_path.write_text(
        result.report.model_dump_json(indent=2),
        encoding="utf-8",
    )
    report_path.write_text(render_monitor_report(result), encoding="utf-8")
    return result
