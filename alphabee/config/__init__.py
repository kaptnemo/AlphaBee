from pydantic import BaseModel, Field

from alphabee.config.loader import ConfigLoader


class LLMConfig(BaseModel):
    api_key: str
    base_url: str
    model: str
    proxy_url: str | None = None
    # 结构化输出开关（json_object 容器约束）：端点能力见 utils/llm.py 模块注释。
    structured_json: bool = Field(default=True, description="直连 LLM 调用点是否绑定 response_format=json_object")


class TavilyConfig(BaseModel):
    api_key: str = ""
    base_url: str = "https://api.tavily.com"
    proxy_url: str | None = None
    timeout: float = Field(default=15.0, description="请求超时秒数")
    max_results: int = Field(default=6, description="默认返回结果数")


class DDGSConfig(BaseModel):
    proxy_url: str | None = None
    timeout: int = Field(default=20, description="请求超时秒数")
    region: str = Field(default="cn-zh", description="搜索区域，如 cn-zh, us-en")
    max_results: int = Field(default=6, description="默认返回结果数")


class WebSearchConfig(BaseModel):
    tavily: TavilyConfig = Field(default_factory=TavilyConfig)
    ddgs: DDGSConfig = Field(default_factory=DDGSConfig)


class LangfuseConfig(BaseModel):
    enable: bool = False
    public_key: str = ""
    secret_key: str = ""
    base_url: str = "http://localhost:3000"


class DataConfig(BaseModel):
    root_dir: str = Field(default="data", description="数据产物根目录")


class DeviationDetectionSettings(BaseModel):
    """§14.6 检测开关：检测器总开关（回滚用）+ v1 恒 false 的 LLM 检测器。"""

    enabled: bool = Field(default=True, description="检测器总开关（关闭即整层 no-op，回滚用）")
    llm_detectors: bool = Field(default=False, description="v1 恒 false（§16：不做 LLM 检测器）")


class DeviationBudgetSettings(BaseModel):
    """§10 偏离预算：严重度权重 + 代价敞口阈值（供 recovery 的直接升级分支使用）。"""

    severity_weight: dict[str, int] = Field(
        default_factory=lambda: {"low": 1, "medium": 3, "high": 10, "critical": 30},
        description="§10.1 代价敞口用的严重度权重",
    )
    cost_exposure_threshold: int = Field(default=30, description="代价敞口超过该值 → 直接升级（T5）")


class DeviationLedgerSettings(BaseModel):
    """§5.2 账本写入开关（fail-open）。"""

    enabled: bool = Field(default=True, description="账本写入总开关（关闭则只记 Step，不写库）")
    fingerprint_normalize: bool = Field(default=True, description="指纹是否先归一化数字/路径再计算")
    retention_days: int = Field(default=365, description="账本保留天数（purge_before 用）")


class DeviationAmplificationSettings(BaseModel):
    """§8 放大审计开关（F3 使用）。"""

    audit_enabled: bool = Field(default=True, description="是否执行 audit_amplification")
    overturn_severity: str = Field(default="high", description="方向不一致时的上报严重度")


class DeviationRecoverySettings(BaseModel):
    """§7 恢复阶梯开关（F2 使用）。

    **覆盖面的显式契约（t55 定稿，captain 裁定方案 (B)「纯回滚」）**：``enabled=False`` 时
    报告 gate 的"是否回环"回落 pre-F2 基线谓词 ``rewrite_needed and used < limit``
    —— **回环能力保留**、``RunStatus`` 与 pre-F2 一致（要重写却没回环 ⇒ ``PARTIAL``）、
    且**不新增** D5 ``budget_exhausted`` 记录（D5 生产者本身是 F2 新增物；关掉 F2 开关却
    新增记录就不构成回滚）。裁决异常亦 fail-open 到同一基线行为。

    该开关 gate 的范围（F2-7 修正，(B) 口径的第三处）：F2 的**阶梯裁决路径、回环能力**，
    以及**预算耗尽 D5（``budget_exhausted``）的产出** —— ``enabled=False`` 时三者一并回到
    pre-F2（**不新增**该记录）。其它检测器的产出由 ``deviation.detection`` 开关负责；
    所有 Issue 的**持久化**由 ``deviation.ledger`` 开关负责。
    （此前本段曾写"开关只 gate 阶梯裁决与回环能力 / 记录由 detection 负责 / 回滚时如需静音
    可关账本开关" —— 三处均不准：(B) 下该记录根本不产生，照旧文本操作会**无必要地关闭账本**，
    反而静音全部偏离记录。）
    依据：`docs/design/DEVIATION_CONTROL_FRAMEWORK.md` §14.8 PR5「纯回滚」。
    """

    enabled: bool = Field(default=True, description="是否启用统一阶梯裁决（关闭 = 纯回滚到 pre-F2 基线行为）")


class DeviationSettings(BaseModel):
    """偏离控制框架的配置总段（§14.6）。**五段全部带默认值**，故配置缺失也能构造。"""

    detection: DeviationDetectionSettings = Field(default_factory=DeviationDetectionSettings)
    budget: DeviationBudgetSettings = Field(default_factory=DeviationBudgetSettings)
    ledger: DeviationLedgerSettings = Field(default_factory=DeviationLedgerSettings)
    amplification: DeviationAmplificationSettings = Field(default_factory=DeviationAmplificationSettings)
    recovery: DeviationRecoverySettings = Field(default_factory=DeviationRecoverySettings)


class Settings(BaseModel):
    llm: LLMConfig
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    # 偏离控制框架（F2 起真实可用；此前各读取点 fail-open 取默认值）
    deviation: DeviationSettings = Field(default_factory=DeviationSettings)


def get_settings() -> Settings:
    config_loader = ConfigLoader()
    raw_cfg = config_loader.load_config()
    return Settings(**raw_cfg)


settings = get_settings()
