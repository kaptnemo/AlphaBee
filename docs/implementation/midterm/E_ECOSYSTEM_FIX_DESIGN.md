# E 数据生态细化实施设计（预告类型加权 + 区间保守锚 + 东财盈利预测数据源）

> **状态：设计文档（2026-10 完成，尚未实施）**。本设计收口 `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §2.2「数据生态假设」的**残留项**：① `forecast_type` 加权未做（评审 P1-2 剩余部分——`update_flag` 映射与同报告期取 `ann_date` 末条已在 P0 PR-A `bb7c06b` 落地）；② 预告区间中点按符号计证仍是近似口径（评审 §2.2 残留）；③ 数据源未升级到东财盈利预测（rating 右偏、target_price 稀疏的根因仍在——P0-6 护栏只是"不信"，不是"有了更好的数据"）。
> 关联文档：`docs/design/A_SHARE_FRAMEWORK_REVIEW.md`（§2.2 展开版、§6 P1-2）、`docs/implementation/framework/P0_A_SHARE_FIX_IMPLEMENTATION_DESIGN.md`（§9 P0-6 已落地部分）、`docs/midterm/MIDTERM_EVIDENCE_EXTRACTION.md`（证据规则基线）、`docs/midterm/MIDTERM_DECISION_MODEL.md`（E 因子口径）。
> 实施时：canonical 字段/新数据源映射走 `alphabee-schema-steward`；外部接口先冒烟后写码走 `data-source-contract` skill；行为变更按 ROADMAP 变更登记表登记。

---

## 1. 背景与目标

P0-6（`494b084`）已把 E 主轴从研报修正重调为「0.6×预告锚 + 0.4×研报修正」并加 coverage<3 双护栏。但三个残留问题使 E 引擎仍未充分吃透 A 股预告生态：

1. **预告类型信息被丢弃**：`type`（预增/预减/扭亏/首亏/续盈/续亏/略增/略减/不确定）经 adapter 已映射为 canonical `forecast_type` 进入记录，但 `evidence_rules` **零消费**（grep 全模块无引用）——"扭亏/首亏"（状态反转，信息熵最高）与"续盈/续亏"（惯性持续，边际信息量最低）被当同一类事件计同样的 confidence_delta。
2. **区间中点口径偏乐观**：符号定方向退化路径用区间**中点**判定方向与幅度（`_forecast_evidence`），而 A 股预告普遍偏乐观（管理口径常给宽上限），中点系统性高估；下限才是可得的最保守预期。
3. **研报聚合仍是 E 的数据底座**：coverage 护栏修的是"不信"，数据源本身（东财研报列表聚合）的 rating 右偏、target_price 稀疏、coverage 口径粗糙（研报数≠机构数）未动。

**目标**：A/B/C 三项，全部 add-on、缺省行为可配置、fail-open；不回调 P0-6 的 0.6/0.4 权重（§10 非目标）。

---

## 2. 现状核实（2026-10-18，全部代码锚点）

| 事实 | 锚点 |
|---|---|
| adapter 在 helper 层已应用：`type → forecast_type`、`end_date → period`、`p_change_min → profit_forecast_min_change`、`update_flag → forecast_update_flag`（P0 PR-A 补）已进记录；`ann_date` 无映射保持原始名 | `collectors/tushare/helper.py:42`（`TuShare_Adapter.adapt(collection_name, data)`）、`adapters/tushare/expectation_mapping.yaml` |
| `evidence_rules` 不消费 `forecast_type`（全模块零引用） | `alphabee/midterm/evidence_rules.py`（grep 实证） |
| 符号定方向退化路径用 `_midpoint(lo, hi)` 判定方向、`_calibrate_beat(midpoint)` 定幅度 | `evidence_rules.py:376-392`（`_forecast_evidence`） |
| beat/miss 分档：<5 weak / 5–15 medium / >15 strong；revision：<3 / 3–10 / >10 | `evidence_rules.py:141-153`（`_calibrate_beat`/`_calibrate_revision`）、常量 `:51-56` |
| 同报告期按 `ann_date` 升序取末条、`forecast_update_flag` 末条 ⇒ retraction 事件——已落地（P0 PR-A） | `evidence_rules.py` `_index_by_period`、`_forecast_retraction_evidence` |
| consensus 现状 = 东财**研报列表**聚合：`coverage_count` 由研报数统计（`_coverage_count`）、`as_of_date` 已带（P0-1 用它做事件日） | `collectors/consensus/engine.py:203/232/241`、`collectors/consensus/eastmoney.py:62-88`（`build_consensus`） |
| 数值通道 fail-open 已有：`collect_evidence_split` 的 try/except 包住 `build_numeric_evidence` | `midterm/decision_model.py:505-512` |
| config 已有 `midterm` 段（P0 TEF PR-1 落地 `classifier_order`） | `alphabee/config/__init__.py:342-365` |

---

## 3. 设计 A：预告类型分档（纯规则，零新数据源）

### 3.1 类型分档表（`evidence_rules.py` 模块常量，结构性示意）

```python
# 预告类型信息量分档（P0 评审 §2.2 / P1-2；tushare forecast.type 取值域）
_FORECAST_TYPE_TIER: dict[str, str] = {
    "扭亏": "reversal",   # 状态反转：上年亏损→盈利，信息熵最高（强制披露 + 方向反转）
    "首亏": "reversal",   # 状态反转：上年盈利→亏损
    "预增": "magnitude",  # 幅度变化：常规信息量
    "预减": "magnitude",
    "略增": "magnitude",
    "略减": "magnitude",
    "续盈": "inertia",    # 惯性持续：方向与上年一致，边际信息量低（已部分被上年财报定价）
    "续亏": "inertia",
    # 缺失 / 不确定 / 未知取值 → "unknown"（fail-open，按现状行为）
}
_DEFAULT_TIER = "unknown"

def _forecast_tier(rec: dict[str, Any]) -> str:
    """读 canonical ``forecast_type``（adapter 已映射）；缺失/异常 → ``_DEFAULT_TIER``。"""
```

### 3.2 三处应用点（全部在 `evidence_rules.py`，离散、可测、不破坏三值纪律）

| 应用点 | 现状 | 新规则 | 边界 |
|---|---|---|---|
| **① `_express_evidence` 的 beat/miss 强度** | `strength=_calibrate_beat(dev)` | 按 tier 升降一档：`reversal` 升一档（weak→medium→strong）、`inertia` 降一档（strong→medium→weak）、`magnitude`/`unknown` 不动 | 升档在 STRONG 封顶、降档在 WEAK 不取消（仍产事件）——升降后**仍在三值域内**，`confidence_delta` 三值纪律不破 |
| **② `_forecast_evidence` 符号定方向** | 中点符号 + `_calibrate_beat(midpoint)` | `inertia` ⇒ **不产事件**（方向与上年一致 = "无意外即无证据"原则的延伸，§5 原则）；`reversal` ⇒ 方向锚用保守口径（§4）；`magnitude`/`unknown` ⇒ 现行行为 | `inertia` 不产是**取消**而非降权——理由：续盈/续亏预告的方向已被上年财报定价，事件本身几乎无新信息（登记于模块 docstring） |
| **③ `_forecast_retraction_evidence`** | retraction 事件（作废，不携带方向） | **不受 tier 影响**（作废动作的信息量不因类型变化） | retraction 后按修正记录**重新生成**的 beat/miss/符号事件走 ①/② 的新规则 |

### 3.3 类型分档的语义依据（写入模块 docstring）

- **reversal（扭亏/首亏）**：A 股扭亏/首亏预告是**强制披露**（制度保证及时性），且是"状态反转"而非"幅度变化"——反转一旦兑现，盈利预期将重定价（0 → 正），信息熵显著高于同幅度的预增；升一档反映"同幅度下反转更值得信"。
- **inertia（续盈/续亏）**：方向与上年一致，预告确认的是市场**已经定价**的方向；降档/不产反映"惯性不产生意外"。
- **magnitude（预增/预减/略增/略减）**：现行行为（区间 beat/miss 与符号退化本来就是为这类设计的）。

---

## 4. 设计 B：区间保守锚（符号定方向口径细化）

### 4.1 规则（`_forecast_evidence` 内，替代 `_midpoint` 路径）

```python
def _conservative_direction(lo: float | None, hi: float | None, tier: str) -> tuple[EffectOnThesis | None, float | None]:
    """符号定方向的保守锚（P0 评审 §2.2 残留 ②）：

    - ``lo > 0`` ⇒ confirming，幅度 = |lo|（下限为正 ⇒ 最坏情形也增长）；
    - ``hi < 0`` ⇒ refuting，幅度 = |hi|（上限为负 ⇒ 最乐观情形也下滑）；
    - 跨零（``lo <= 0 <= hi``）⇒ 方向不确定：
        ``tier == "reversal"`` 且绝对额下限 ``forecast_net_profit_min > 0`` ⇒ confirming(weak)
        （扭亏区间跨零是制度产物：同比基数为负，下限跨越 0 属常态；以净利润绝对额兜底判定）；
        其余 ⇒ ``(None, None)``（不产事件——区间跨越 0 = 方向不确定，不编造）；
    - 全缺失 ⇒ ``(None, None)``（现状）。
    """
```

- 事件 `value` 记**保守锚值**（lo 或 hi，非中点）；`description` 注明区间与"下限/上限保守锚"口径（自证不猜测）。
- 理由登记：A 股预告区间**普遍偏乐观**（上限给宽裕、实际贴近下限），中点把不确定性平均化并系统性高估；下限（对 confirming）与上限（对 refuting）是可得的最保守预期，且与"反证更有信息量"（bayes `_REFUTING_WEIGHT=1.2`）的方向保守主义一致。

### 4.2 与既有路径的关系

- `_express_evidence` 的 beat/miss（actual vs 预告区间）**不改**——它有实际值，不需要保守锚；
- `_forecast_evidence` 的 `inertia` 类已在 §3.2 ② 取消，保守锚只作用于 `reversal`/`magnitude`/`unknown`；
- `score_engine._forecast_revision_anchor`（P0-6 的预告锚）**不改**——它比较相邻报告期的**区间中点差**，本设计不动 score 层口径（§10 非目标），两处口径差异在模块 docstring 披露。

---

## 5. 设计 C：东财 F10 盈利预测数据源（冒烟先行）

### 5.1 契约形状（数据源）

东财 F10「盈利预测」页接口（`emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=...`）给出**机构预测汇总**，映射到既有 canonical（全部已注册，无需新字段）：

| 东财字段（实施前按 data-source-contract 冒烟实测为准） | canonical | 语义 |
|---|---|---|
| 预测年度（序列） | 预测年度分组 | fy1/fy2 取自当前年+1/+2 |
| 机构数 | `coverage_count` | **机构家数**（比研报数更接近真实覆盖） |
| EPS 均值/最高/最低 | `eps_fy1`/`eps_fy2`（均值） | 一致预期 EPS |
| 净利润均值 | （FY1/FY2 净利，供预测锚对照） | 可选 |
| 评级分布（若接口含） | `rating_mean`/升降级计数 | 比研报列表聚合更真实的评级面 |
| 目标价（若接口含） | `target_price` | 覆盖度待冒烟确认；无则维持现状来源 |

### 5.2 快照时序与 revision 自算

新源只给**当前快照**（无历史时序），而 `eps_fy1_revision_1m/3m` 需要时序。方案（与 midterm persistence 同构）：

- 新模块 `alphabee/collectors/consensus/emforecast.py`：`fetch_profit_forecast(code) -> dict[str, Any]`（单次快照，fail-open 返回 `{}`）+ `append_snapshot(symbol, values)` 落盘 `data/consensus_snapshots/<symbol>.jsonl`（append-only，每行 `{as_of_date, eps_fy1, eps_fy2, coverage_count, ...}`，损坏行跳过、同日幂等）；
- revision 计算复用 `engine.py` 既有的窗口模式（`_revision_series`/`_daily_median_series` 同构）：从快照序列取 1m/3m 前的 eps_fy1 均值，算变化率——**纯规则、无 LLM、可离线重放**。

### 5.3 provider 链与切换（`build_consensus` 改造）

```python
def build_consensus(code, *, months=12, ..., consensus_source: str | None = None) -> ConsensusOutput:
    """共识聚合 provider 链（E 生态设计 §5.3）：
    - consensus_source 缺省 → 读 settings.midterm.consensus_source（fail-open）；
    - "report_list" ⇒ 现状研报聚合路径（逐字不变，source="eastmoney:report/list"）；
    - "f10_forecast" ⇒ emforecast 快照 + revision 自算（source="eastmoney:f10/profit_forecast"），
      失败 ⇒ 回落 report_list 并在 ConsensusOutput.warnings 留痕；
    - 非法值 ⇒ warn + "report_list"（绝不抛）。
    """
```

- config 新增：`midterm.consensus_source: Literal["report_list", "f10_forecast", "auto"] = "report_list"`（**默认 report_list = 现状零行为变化**；`auto` = f10 优先回落；`f10_forecast` = 只用新源失败回落）。`config.yaml`/`config.yaml.example` 同步 + ROADMAP 登记。
- 调用方接线：`collect_evidence_split`（`decision_model.py:505-512`）的 `build_consensus(symbol)` 调用**不改**（provider 链内部分派）；`factors.build_expectation_factor` 的 consensus 读取同路径自动生效。
- 冒烟前置（data-source-contract 纪律，PR-B 开工前提）：对 3 个样本（沪主板大市值、深主板中市值、创业板小市值各一）实测接口：确认入参（code 格式）、出参字段名与空值形态（无覆盖/停牌/次新）、分页与否、是否需 UA/Referer 头；冒烟结果写入 ROADMAP 登记行或模块 docstring，**字段名不符时以实测为准改映射，不凭接口名猜测**。

### 5.4 与新源相关的下游语义变化（显式登记）

- `coverage_count` 语义由"研报数"变"机构数"：P0-6 的 `<3` 护栏判据**不变**（机构数<3 同样不可信），但历史帧的 coverage 值不可与新源帧直接混比（与 TEF provenance 同类的口径问题——对比实验只在新源启用后的帧内进行）；
- `ConsensusOutput.source` 自证来源，报告/账本可审计；
- revision 事件日继续用 `consensus.as_of_date`（P0-1 口径不变）：新源快照的 as_of = 快照写入日。

---

## 6. 兼容与影响

| 面 | 影响 | 处置 |
|---|---|---|
| evidence 事件强度 | A（升降档）改变部分事件的 `confidence_delta` 档位 | **行为变更**：ROADMAP 登记；受影响既有测试按新档位更新预期（§7 列清单） |
| evidence 事件数量 | B（inertia 不产、跨零不产）减少事件流 | 行为变更登记；`evidence_arrival_rate` 相应下降（监控触发口径不变，只影响事件数） |
| 帧/artifact | 事件结构、`CompanyStateArtifact` 字段面**零变化** | 下游 bayes/diff/reporter 零改动 |
| consensus 路径 | C 在默认 `report_list` 下零变化 | 新源启用是显式配置动作，默认行为逐字不变 |
| 既有测试 | `tests/midterm/test_evidence_rules.py` 中符号退化/分档用例 | 逐条按新口径更新（§7 测试矩阵给出替换用例） |

---

## 7. 测试矩阵

| 文件 | 用例 | 断言 |
|---|---|---|
| `tests/midterm/test_evidence_rules.py`（增补） | A-升降档 | 扭亏 beat 偏离 4% ⇒ medium（升档）；续亏 beat 偏离 4% ⇒ weak（降档）；预增 ⇒ weak（不动）；STRONG 升档封顶、WEAK 降档仍产 |
| 同上 | A-inertia 取消 | 续盈预告（无快报）⇒ 不产 forecast 事件；预减同区间 ⇒ 产 |
| 同上 | A-retraction 不受 tier | 续盈类修正预告 ⇒ retraction 事件照常产 |
| 同上 | B-保守锚 | lo=+5/hi=+40 ⇒ confirming 且 value=5；lo=−50/hi=−5 ⇒ refuting 且 value=−5；lo=−20/hi=+30 ⇒ 不产；扭亏跨零且 net_profit_min>0 ⇒ confirming(weak) |
| 同上 | 未知/缺失 tier | `forecast_type` 缺失 ⇒ 现行行为（回归钉） |
| `tests/collectors/test_emforecast.py`（新） | C-冒烟 fixture | 合成东财响应 → canonical 映射逐字段；空值形态（无覆盖/停牌）→ 缺失登记不抛 |
| 同上 | C-快照时序 | append 幂等（同日同值一行）；1m/3m revision 由快照序列正确计算；坏行跳过 |
| 同上 | C-provider 链 | `f10_forecast` 失败 ⇒ 回落 report_list + warnings 留痕；`auto` 优先新源；非法值 ⇒ warn + report_list；默认（缺段）⇒ report_list 逐字现状 |
| `tests/midterm/test_score_engine.py`（回归） | P0-6 不动 | coverage 护栏、0.6/0.4 锚重调行为逐字不变 |

**变异杀伤**：删 tier 分档 ⇒ 升降档钉必红；把保守锚改回中点 ⇒ 保守锚钉必红；provider 链去掉 fallback ⇒ 失败回落钉必红。
**全量回归**：`pytest tests -q -m "not integration"` 离线口径；`tests/midterm`/`tests/collectors` 全绿。

---

## 8. PR 切分与回滚

- **PR-A（类型分档 + 保守锚，§3/§4）**：纯规则、零新数据源、零新依赖——收口评审 P1-2 与 §2.2 残留 ②。回滚 = 还原三处应用点（`git revert` 单提交）。
- **PR-B（东财 F10 盈利预测，§5）**：冒烟先行（§5.3 前置条件），含新模块 + 快照落盘 + config 段。回滚 = 删除 `emforecast.py` + 快照目录（`data/consensus_snapshots/` 可留作审计资产）；`midterm.consensus_source` 默认 `report_list` ⇒ 配置面零行为变化。

---

## 9. 验收标准

1. **A 分档**：三档升降在 beat/miss 与符号路径逐条生效；inertia 取消符合"无意外即无证据"；retraction 不受 tier 影响；`confidence_delta` 恒在三值域（三值纪律专测）；
2. **B 保守锚**：下限/上限锚、跨零三档、扭亏绝对额兜底逐条成立；事件 `value`/`description` 自证保守口径；
3. **C 数据源**：冒烟结果登记后，canonical 映射与快照时序正确；provider 链三种 source 模式 + fallback + fail-open 全测；**默认 `report_list` 下全链路行为逐字不变**（回归钉）；
4. **零下游改动**：`CompanyStateArtifact`/`EvidenceEvent` 字段面不变，bayes/diff/reporter/consumers 零改动且全绿；
5. **登记**：ROADMAP 变更登记表（A/B 行为变更、`midterm.consensus_source` 默认值、coverage 语义变化）；模块 docstring 披露（三档表出处、保守锚理由、inertia 取消理由）。

---

## 10. 非目标（防范围膨胀）

- **不回调 P0-6 的 0.6/0.4 权重**（新源落地后经回测再议，属另一次行为变更登记）；
- 不做评级（rating）加权与目标价覆盖率的专项修复（随新源自然改善的部分登记观察，不做额外规则）；
- 不用东财 datacenter-web 全量报表接口（RESPREDICT 等）——本设计只用 F10 盈利预测页一个入口；
- 不动 `score_engine._forecast_revision_anchor` 的中点口径（§4.2 披露差异）；
- 不做盈利预测历史回填（快照从启用日起积累，历史 revision 仍走研报路径或为 None）。

---

## 11. 文档登记要求

1. `ROADMAP.md` 变更登记表两行（PR-A 行为变更：类型分档/保守锚；PR-B 配置与语义：`midterm.consensus_source` 默认 `report_list`、`coverage_count` 机构数口径）；
2. `docs/design/A_SHARE_FRAMEWORK_REVIEW.md` §2.2 残留段与 §6 P1-2 追加「已实施」标注；
3. `docs/guide/CONFIGURATION.md` 增补 `midterm.consensus_source` 说明；
4. schema-steward 条目：本设计**无新 canonical 字段**（全部复用既有），仅登记新数据源映射表（东财字段 → 既有 canonical）备查。
