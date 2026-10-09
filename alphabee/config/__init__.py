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
    """§10 偏离预算：严重度权重 + 代价敞口阈值 + 每任务类型的 ``D_max``（§10.2 / §14.6）。"""

    severity_weight: dict[str, int] = Field(
        default_factory=lambda: {"low": 1, "medium": 3, "high": 10, "critical": 30},
        description="§10.1 代价敞口用的严重度权重",
    )
    cost_exposure_threshold: int = Field(default=30, description="代价敞口超过该值 → 直接升级（T5）")
    d_max: dict[str, int] | int | None = Field(
        default_factory=lambda: {"analysis": 60, "tracking": 20},
        description=(
            "§14.6 / §10.2 每任务类型的偏离预算 D_max —— §11.1「预算消耗 = D_cum / D_max」的分母。"
            "按 run.context['task_kind'] 取键，缺省 'analysis'。**必须带默认值**：本模块有模块级 "
            "``settings = get_settings()``，缺 ``deviation`` 段的 config.yaml 不得在 import 期抛错。"
            "类型放宽为 dict | int | None：标量（整段预算）与缺失（None）都被接受（避免配置畸形在 "
            "import 期抛错）；解析仍按 telemetry._budget_limit 的既有顺序，缺失/非正/不可解析一律 "
            "None（不回退 0，见 §14.5-B）。"
        ),
    )


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


class DeviationTrackingSettings(BaseModel):
    """跟踪环（F4）+ 入口前置校验（P2 / D2-B2）的共享配置段（§14.6 / §15.2-D / §15.7）。

    **本段在 P2 之前并不存在** ⇒ ``tracking/triggers.py::thresholds_from_settings()`` 一直走
    ``_config_section() is None`` 分支（``TriggerThresholds()`` 全默认、``tv_distance=None``
    ⇒ ``monitor_kwargs()`` 返回 ``{}``、由 ``monitor_triggers`` 用自身默认常量）。本段落地后，
    该函数开始**显式**读出下列值；除 ``block_stale_runs`` 属**新增行为**外，其余均为
    **既有默认的显式登记（有效判定不变）**：

    * ``stale_after_days=7`` == ``tracking.scheduler.DEFAULT_STALE_AFTER_DAYS``（帧写入侧的保鲜期）；
    * ``tv_distance=0.3`` == ``midterm/diff_consumers.py::_TV_TRIGGER`` —— 落地后
      ``monitor_kwargs()`` 由"不传参"变为显式传 ``tv_threshold=0.3``；因 ``monitor_triggers``
      的默认值**就是**同一常量，**有效判定逐字不变**（有专测
      ``test_tracking_thresholds_explicit_registration_keeps_monitor_verdict`` 在真实 diff 上对拍）；
    * ``evidence_rate=0.5`` == ``_EVIDENCE_RATE_TRIGGER``（当前无读取点，属显式登记）；
    * ``max_alerts_shown=20`` 只服务 ``--track-alerts`` 只读视图（无数值判定语义）。

    ``block_stale_runs=true`` 会让 CLI 交互入口在"该标的最新帧已陈旧 / 存在未消费触发"时
    **拒绝执行**（``SystemExit(3)``；``--allow-stale`` 显式放行，放行后仍由 ``collect_raw_facts``
    记账）；``false`` ⇒ 只记录不阻断。**行为变更登记见 ``docs/roadmap/ROADMAP.md``「行为变更登记」
    P2 行**。

    缺该段的旧 ``config.yaml`` 仍可 import（本段带默认值）：未配置者拿到的是"既有默认的显式登记"
    这一等价结果 —— 唯一例外是 ``block_stale_runs=true`` 的新阻断行为，已登记。
    """

    stale_after_days: int = Field(
        default=7,
        description="数据保鲜期（天）：帧 stale_after 到期即视为陈旧（显式登记既有默认 7）",
    )
    block_stale_runs: bool = Field(
        default=True,
        description="入口是否阻断陈旧/未对账状态下的 CLI 分析请求（false = 只记录不阻断）",
    )
    tv_distance: float = Field(
        default=0.3,
        description="信念位移阈值（显式登记既有默认，== midterm.diff_consumers._TV_TRIGGER）",
    )
    evidence_rate: float = Field(
        default=0.5,
        description="证据到达率阈值（显式登记既有默认，== midterm.diff_consumers._EVIDENCE_RATE_TRIGGER）",
    )
    max_alerts_shown: int = Field(
        default=20,
        description="--track-alerts 只读视图每标的显示的最大告警帧数",
    )


class DeviationSettings(BaseModel):
    """偏离控制框架的配置总段（§14.6）。**各段全部带默认值**，故配置缺失也能构造。"""

    detection: DeviationDetectionSettings = Field(default_factory=DeviationDetectionSettings)
    budget: DeviationBudgetSettings = Field(default_factory=DeviationBudgetSettings)
    ledger: DeviationLedgerSettings = Field(default_factory=DeviationLedgerSettings)
    amplification: DeviationAmplificationSettings = Field(default_factory=DeviationAmplificationSettings)
    recovery: DeviationRecoverySettings = Field(default_factory=DeviationRecoverySettings)
    # 研究连续体 P2（D2-B2）：跟踪阈值 + 入口阻断开关（缺段 ⇒ 上列默认；旧 config 仍可 import）
    tracking: DeviationTrackingSettings = Field(default_factory=DeviationTrackingSettings)


class ReportWindowSettings(BaseModel):
    """§15.1-F 财报原文窗口（研究连续体 P1 / W3）：本地已解析财报 → 证据抽取窗口的预算与开关。

    **默认 ``enabled=True`` 会改变 run 的可观测面**（仅当本地存在该标的已解析财报时窗口
    内容才变化；无报告时新增一条 D1 ``report_window_unavailable`` issue）⇒ 属行为变更，
    **行为变更登记见 ``docs/roadmap/ROADMAP.md``「行为变更登记」P1 行**（已落笔）。
    回滚方式：``enabled: false`` —— 该开关**同时**门控窗口内容与 D1 记账，即
    ``_window_texts()`` 逐字回到旧实现 ``_conflict_explanations(artifacts) or None`` 且不读盘。

    **可复算口径（对拍用例不 stub ``select_report_window``，直接跑真实代码路径）**：

    * 开关透传 + 内容逐字等价（用例内置反 stub 哨兵：``select_report_window`` 一旦被真正
      走到就会触发 ``reports_root`` 的 ``AssertionError``）::

          poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
              pytest tests/orchestrator/test_report_window.py -k switch_off -q
          # 期望：1 passed（test_switch_off_window_texts_equal_legacy_and_no_side_effects）

    * 开关关闭时**不读配置**（``enabled=False`` 短路位于任何读配置/读盘之前）::

          poetry run env HOME=/data/freedom/AlphaBee/tmp/pytest_home \
              pytest tests/orchestrator/test_report_window.py -k disabled_selection_reads_nothing -q
          # 期望：1 passed

    若把该对拍用例**改回 stub ``select_report_window``**，则 M5 型变异（开关未透传进窗口
    选择，即"开关只门控 D1 记账、不门控窗口内容"）不被杀死 ⇒ 上述两处期望值即判据。
    """

    enabled: bool = Field(default=True, description="窗口总开关（false ⇒ 完全回到现状：只喂冲突解释）")
    max_chars: int = Field(default=12_000, description="窗口字符预算（约 8k token 量级）")
    max_sections: int = Field(default=12, description="窗口章节数预算")
    reports_root: str | None = Field(
        default=None,
        description="报告根目录；null ⇒ report_parser.reports_root() 默认（<PROJECT_ROOT>/reports）",
    )


class PeerQualityConfidenceSettings(BaseModel):
    """对标组置信度（设计 §3.7）：三项权重 + 三档阈值（缺段回落默认）。

    确定性合成（不额外调 LLM）::

        confidence_score = w_taxonomy · taxonomy_reliable       # E：target L3 是否可信
                         + w_judge_direct · judge_direct_ratio  # D：保留项中 verdict=direct 占比
                         + w_overlap · mean_overlap             # C：保留项 overlap 均值

    档位：``< low → 低``、``< medium → 中``、否则 ``高``；**缺失信号按 0 处理**。

    **D 项口径（`judge_direct_ratio`）**：`judge_enabled=false`（当前默认）或 judge 不可用时，D 项按 **0** 计入 ⇒ 档位只反映 E（`taxonomy_reliable`）与 C（`mean_overlap`）两路信号。
    """

    low: float = Field(default=0.4, description="低于该值 ⇒ 低置信（报告显式提示基准参考性弱）")
    medium: float = Field(default=0.7, description="低于该值 ⇒ 中置信；否则高置信")
    weights: dict[str, float] = Field(
        default_factory=lambda: {"taxonomy_reliable": 0.4, "judge_direct_ratio": 0.3, "mean_overlap": 0.3},
        description=(
            "三项权重（和 = 1；缺键按 0 处理，多余键忽略）；"
            "`judge_enabled=false`（默认）时 D 项（judge_direct_ratio）按 0 计入 ⇒ 档位只反映 E 与 C 两路信号"
        ),
    )


class PeerQualitySettings(BaseModel):
    """对标组候选质量闸（判定 C）与独立 batch judge（判定 D，设计 §3.3/§3.5/§3.6）。

    读写口径：权重/阈值的**唯一处**在 ``alphabee/company_track/peer_judge.py`` 的默认常量，
    本段用于**覆盖**；缺段 ⇒ 全部取默认值（旧 config 仍可 import，行为不变）。

    阈值来源（设计 §8 决策 3）：由标注集 **train 段**标定，生产生效阈值 = 标定点
    （标定点与 holdout 验收数字见 `outputs/peer_group_eval.md` 的 DoD 复核章节）。
    """

    enabled: bool = Field(
        default=True,
        description=(
            "候选质量闸总开关（设计 §3.5）。false ⇒ 闸门**不做任何剔除**：全部候选直接保留"
            "（含 verdict=reject 与低分/缺分候选），仅在 notes 记一行「质量闸已停用（peer_quality.enabled=false）」；"
            "数值与维度仍照常记分。**只门控本闸**，不影响消费侧 min_peers 闸、taxonomy_enabled 与置信度三档"
        ),
    )
    min_overlap: float = Field(default=0.40, description="合成 overlap 下限（0–1；train 段标定）")
    weights: dict[str, float] = Field(
        default_factory=lambda: {"product": 0.40, "customer": 0.30, "business_model": 0.20, "material_tech": 0.10},
        description="四维权重（overlap = Σ w_i·dim_i）",
    )
    product_floor: float = Field(default=0.20, description="product 维度下限（train 段标定）")
    customer_floor: float = Field(default=0.20, description="customer 维度下限")
    residual_l3_min_constituents: int = Field(default=15, description="残差桶判定：L3 成分数低于该值 ⇒ 分类学不可信")
    taxonomy_enabled: bool = Field(
        default=True,
        description=(
            "分类学（判定 E）开关：false ⇒ 不读静态快照、不并入召回池、不注入 same_l3/same_l2 特征，"
            "置信度按缺失信号处理（**回滚口径**：分类学相关行为一键停用，Gate 与 min_peers 语义不变）"
        ),
    )
    judge_enabled: bool = Field(
        default=False,
        description=(
            "独立 batch judge 开关（与生成同模型，不新增模型配置项）。**默认 false**：Step 2 的 DoD 复核为"
            "**负结果**——F1 判据在标定点/生产生效点成立（0.702 ≥ 0.685），但稳定性判据不成立"
            "（judge 策略保留集 Jaccard 相对生成器口径 train/holdout/all 三段均略降 −0.009~−0.025）⇒ "
            "按设计 §6 Step 2 的负结果路径**不得默认启用**；接线与配置保留，置 true 即启用（Gate 改消费"
            "judge 的 verdict/dims）。judge 失败/超时/非 JSON/漏判 ⇒ fail-open：回退生成器分继续走 Gate "
            "并记 judge_degraded，绝不已此置 no_peers"
        ),
    )
    judge_batch_size: int = Field(
        default=20, description="judge 单批候选数上限（唯一默认处 = peer_judge.JUDGE_BATCH_SIZE_DEFAULT）"
    )
    min_peers: int = Field(default=2, description="消费侧最小对标数：低于该值不注入 peer_*、回退 industry")
    confidence: PeerQualityConfidenceSettings = Field(default_factory=PeerQualityConfidenceSettings)


class CompanyTrackSettings(BaseModel):
    """公司赛道（COMPANY_TRACK）：对标组候选判定相关配置。"""

    peer_quality: PeerQualitySettings = Field(default_factory=PeerQualitySettings)


class Settings(BaseModel):
    llm: LLMConfig
    langfuse: LangfuseConfig = Field(default_factory=LangfuseConfig)
    web_search: WebSearchConfig = Field(default_factory=WebSearchConfig)
    data: DataConfig = Field(default_factory=DataConfig)
    # 偏离控制框架（F2 起真实可用；此前各读取点 fail-open 取默认值）
    deviation: DeviationSettings = Field(default_factory=DeviationSettings)
    # 研究连续体 P1（W3）：财报原文窗口（缺段 ⇒ 取上列默认值）
    report_window: ReportWindowSettings = Field(default_factory=ReportWindowSettings)
    # 公司赛道：对标组候选判定（判定 C；缺段 ⇒ 全默认值，旧 config 行为不变）
    company_track: CompanyTrackSettings = Field(default_factory=CompanyTrackSettings)


def get_settings() -> Settings:
    config_loader = ConfigLoader()
    raw_cfg = config_loader.load_config()
    return Settings(**raw_cfg)


settings = get_settings()
