"""驱动画像研究代理（Tier B）的结构化输出契约。

业务定位：``DriverProfile`` v2 是**artifact 契约**，字段面为下游消费设计（结构化
observable / 证据 / 优先级）；而 LLM 端点的能力受限（不支持 json_schema 强约束，
见 ``utils/llm.py`` 模块注释），故单独定义一份「LLM 原始输出」schema：

- 只用于 ``json_instruction(DriverResearchOutput)`` 注入 + ``parse_json`` 后的中间态校验；
- 由 ``domain_context.research.converge`` 确定性收敛成 ``DriverProfile``
  （``driver_hypotheses`` / ``research_agenda`` 字段一一对应，``observables`` 先取字符串
  再在收敛层拆分「名称 + 来源」）；
- ``model_config = ConfigDict(extra="ignore")``：LLM 多给字段不炸（端点无 schema 约束时
  模型常自行加字段），缺字段走默认值 + 校验。

``json_schema_extra.example`` 是 ``json_instruction()`` 的 few-shot 示例来源；
改字段时必须同步更新示例，避免 LLM 被过期示例误导。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class ResearchEvidence(BaseModel):
    """一条可回源的证据引用（Tier C 的 C2"证据可溯"判据 = ``ref`` 非空）。"""

    kind: str = ""  # annual_report / segment / tushare / industry_benchmark / web
    ref: str = ""  # 精确定位（章节名 / api+params / URL / 字段名）
    quote: str = ""  # 原文片段（限长）

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "kind": "annual_report",
                "ref": "2025 年报 第三节 管理层讨论与分析 / 报告期内公司从事的主要业务",
                "quote": "报告期内，公司高速覆铜板产品收入占比提升，主要受益于 AI 服务器需求增长。",
            }
        },
    )


class ResearchHypothesis(BaseModel):
    """一条标的特异的驱动假设（LLM 版；``variable`` 必填）。"""

    variable: str  # 驱动变量名（公司特异，不是行业常量）
    role: str = "primary"  # primary / secondary / risk
    mechanism: str = ""  # 传导链：变量 → 中间环节 → 盈利结果
    company_form: str = ""  # 该变量在本公司的具体形态（带分部数值/结构）
    observables: list[str] = Field(default_factory=list)  # 收敛层可简化：字符串列表
    falsifiers: list[str] = Field(default_factory=list)  # 什么情况该变量不再主导
    evidence: list[ResearchEvidence] = Field(default_factory=list)
    confidence: float = 0.0

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "variable": "高频高速 CCL 收入占比 × 铜箔-树脂价差",
                "role": "primary",
                "mechanism": "AI 服务器放量 → 高频高速 CCL 收入占比提升 → 高毛利产品结构改善 → 单位毛利抬升",
                "company_form": "2025 年高速覆铜板收入占比 42%（同比 +18pp），分部毛利率高于公司整体 6.2pp",
                "observables": [
                    "高频高速 CCL 分部毛利率（来源：annual_report:第三节）",
                    "铜箔-树脂价差（来源：tushare:income）",
                ],
                "falsifiers": ["AI 服务器订单增速回落到 20% 以下", "高频高速 CCL 单价同比转负"],
                "evidence": [
                    {
                        "kind": "annual_report",
                        "ref": "2025 年报 第三节 / 报告期内公司从事的主要业务",
                        "quote": "高速覆铜板产品收入占比提升。",
                    }
                ],
                "confidence": 0.72,
            }
        },
    )


class ResearchAgendaItem(BaseModel):
    """一条研究议程条目（必须可执行：写清"什么证据能定论 + 去哪拿"）。"""

    question: str
    why_matters: str = ""
    decisive_evidence: list[str] = Field(default_factory=list)
    preferred_sources: list[str] = Field(default_factory=list)
    priority: str = "high"  # critical / high / medium

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "question": "公司自称 AI 服务器龙头，但该分部收入占比仅 8%——这个占比口径是否包含代工部分？",
                "why_matters": "若口径含代工，则高毛利自研部分的真实占比更低，主线应从该分部切换到真实主力分部。",
                "decisive_evidence": [
                    "年报分产品收入明细表的原始口径（是否按产品还是按客户）",
                    "同口径下代工与自研的收入拆分",
                ],
                "preferred_sources": ["query_financial_report 第三节分产品收入明细", "query_tushare income"],
                "priority": "critical",
            }
        },
    )


class DriverResearchOutput(BaseModel):
    """Tier B 研究层的整体输出（Tier C 的输入）。"""

    profit_formula: str = ""  # 盈利公式（钱从哪来）
    driver_hypotheses: list[ResearchHypothesis] = Field(default_factory=list)
    research_agenda: list[ResearchAgendaItem] = Field(default_factory=list)
    candidates: list[str] = Field(default_factory=list)  # 备选 playbook id（多主线）
    novel_drivers: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)  # 无法验证的猜测（不进主线）

    model_config = ConfigDict(
        extra="ignore",
        json_schema_extra={
            "example": {
                "profit_formula": "收入 ≈ 高速覆铜板销量 × 单价 + 常规覆铜板销量 × 单价；利润 ≈ 收入 − 铜箔/树脂成本 − 折旧",
                "driver_hypotheses": [
                    {
                        "variable": "高频高速 CCL 收入占比 × 铜箔-树脂价差",
                        "role": "primary",
                        "mechanism": "AI 服务器放量 → 高频高速 CCL 占比提升 → 产品结构改善 → 单位毛利抬升",
                        "company_form": "2025 年高速覆铜板收入占比 42%（同比 +18pp）",
                        "observables": ["高频高速 CCL 分部毛利率（来源：annual_report:第三节）"],
                        "falsifiers": ["AI 服务器订单增速回落到 20% 以下"],
                        "evidence": [
                            {
                                "kind": "annual_report",
                                "ref": "2025 年报 第三节 / 报告期内公司从事的主要业务",
                                "quote": "高速覆铜板产品收入占比提升。",
                            }
                        ],
                        "confidence": 0.72,
                    }
                ],
                "research_agenda": [
                    {
                        "question": "高速覆铜板收入占比的提升是否可持续？",
                        "why_matters": "该分部是当前利润弹性的主要来源。",
                        "decisive_evidence": ["下一期分产品收入明细", "主要客户资本开支指引"],
                        "preferred_sources": ["query_financial_report 第三节", "query_tushare income"],
                        "priority": "critical",
                    }
                ],
                "candidates": ["generic_fundamental"],
                "novel_drivers": ["铜箔-树脂价差"],
                "open_questions": ["公司未披露的自研/代工拆分比例"],
            }
        },
    )
