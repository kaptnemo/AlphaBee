# 真实赛道标签「粗口径单桶退化」修复方案

> **状态：📝 方案待评审（未动代码）**
> 背景：工业富联（601138.SH）driver_profile 缓存键为
> `601138.SH|20251231|3C电子产品|1`，其中 `track_label=3C电子产品` 是东财主营构成
> 「按产品分类」退化出的粗口径单桶标签，既不符合设计意图（`context_router.py:17`
> 明示「工业富联 track=AI 服务器 vs 申万=通信设备」），又会让缓存键与 playbook 路由
> 失去「真实赛道」语义。
>
> 修复对象（三步，见 §2）：
> - **Phase 1（方向二）**：`scripts/segment_taxonomy_audit.py` 增加「产品分类粗口径单桶」检测，量化影响面；
> - **Phase 2（方向一）**：`alphabee/company_track/label.py` 选类判据从「固定产品优先」改为「粒度/信息量优先」，并补单测；
> - **Phase 3（方向三）**：`alphabee/company_track/track.py` 增加人工 override 兜底表，收尾规则判不对的个案。
>
> 不动：`research_cache_key` 的 key 组成（symbol|report_period|track_label|playbook_version 不变）、
> `CompanyTrackArtifact` 契约字段、下游消费方。本文档与
> `docs/roadmap/COMPANY_TRACK_ROADMAP.md` Phase B（真实赛道标签推导）对应，是该 Phase 的修正补丁。

---

## 1. 问题与根因

### 1.1 解剖样本：工业富联 601138（20251231）

东方财富主营构成（`ak.stock_zygc_em`，经 `company_track/data.py` 归一）三条分类口径：

| 分类 | 分项（去「其他(补充)」后） | 备注 |
| :--- | :--- | :--- |
| 按产品分类 | `3C电子产品` 99.82% | **单桶**，粗口径，吞掉全部业务 |
| 按行业分类 | `云计算` 66.75%（yoy +88.7%）/ `通信及移动网络设备` 32.99%（yoy +3.5%）/ `工业互联网` 0.08% | 有区分度、与真实驱动一致 |
| 按地区分类 | 中国大陆及其他 55.9% / 墨西哥 34.0% / 越南 9.2% / 新加坡 0.7% | 与赛道无关 |

当前 `derive_track_label`（`label.py:102`）走 `_pick_category`（`label.py:52`），
`_CATEGORY_PREFERENCE = ("按产品分类", "按行业分类")` 固定「产品优先」，
去 other 后产品只剩一根柱子，于是 `track_label = 3C电子产品`。

### 1.2 根因

「分类类型优先」是**静态、无信息量意识**的：它只回答「有产品口径就用产品」，不回答
「这个产品口径本身是否退化成粗大类单桶」。工业富联的「按产品分类」只有一个大杂烩桶
`3C电子产品`（99.82%），而「按行业分类」反而更细——静态优先会选错。

> 实证边界（内置样本全量审计，见 §3.1）：粗口径单桶退化目前**只**在工业富联命中。
> 立讯精密（002475.SZ）虽曾被怀疑，但其当前「按产品分类」与「按行业分类」是同一组
> 3 项（消费电子 70.19% / 汽车电子 18.56% / 通讯及数据中心 9.52%），不存在「产品粗、
> 行业细」错配，**不属于受影响样本**，不得作为举例或预期回退样本。

### 1.3 设计难点：单桶 ≠ 错

**不能**一刀切「产品单桶就回退行业」。反例：

- 中芯国际（688981.SH）「按产品分类」也是单桶 `集成电路晶圆制造代工`，但语义精确、
  就是正确赛道；
- 工业富联单桶 `3C电子产品` 是粗大类，把云计算/通信设备/工业互联网全吞了。

所以判据必须能区分「单桶且语义精确」与「单桶且粗口径退化」（见 §3.2）。

---

## 2. 修复总览（三步走 + 依赖关系）

| 阶段 | 对应方向 | 目标 | 是否强依赖前一步 | 可否提前 |
| :--- | :--- | :--- | :--- | :--- |
| Phase 1 | 方向二 | 加检测、量化影响面 | — | — |
| Phase 2 | 方向一 | 改选类判据 + 补单测（根治） | **依赖 Phase 1 的量化结果定判据宽窄** | 否 |
| Phase 3 | 方向三 | 人工 override 兜底（长尾） | 不依赖 Phase 1/2 | **可随时插入止血** |

**依赖关系**：Phase 1 → Phase 2 是强依赖（先量后修）；Phase 3 是兜底，可并行/提前。
若当前只救工业富联一个点，最省事顺序是「Phase 3 止血 → Phase 1 确认范围 → Phase 2 根治」。

每个 Phase 独立可回滚、独立可验收（见各节「验收标准」）。

---

## 3. Phase 1（方向二）：审计加检测、量化影响面

> 目标：不改生产逻辑，先用审计脚本回答「多少家公司中招、影响多大、判据要多宽」。
> 产出物是 Phase 2 的**决策依据**，本身不改变任何线上标签/缓存。

### 3.1 改动范围

仅 `scripts/segment_taxonomy_audit.py`（`_analyze`，第 184 行起的 flags 汇总区）：
新增一个失效模式 `COARSE_PRODUCT_BUCKET`，复用现有 `by_period_cat` / `_informative`
基础设施，不新增取数。

### 3.2 判据定义（审计侧先用手写的宽松判据）

对一个报告期，若同时满足：

1. 「按产品分类」去 other 后有效分项数 == 1（或 top1 占比 ≥ 95%）；
2. 「按行业分类」去 other 后有效分项数 ≥ 2，且 top1 占比 < 95%（说明行业口径有区分度）；

则标记 `COARSE_PRODUCT_BUCKET`。同时输出「若按行业分类，会得到什么标签」
（用 `derive_track_label` 对行业口径重跑一遍，仅作对照，不改生产）。

> 中芯国际：产品 1 项、行业 1 项 → 条件 2 不满足 → 不误报。
> 工业富联：产品 1 项、行业 3 项且 top1=66.75% → 命中。

### 3.3 验收标准

- [ ] 脚本对内置样本跑通，工业富联命中 `COARSE_PRODUCT_BUCKET`，中芯国际**不**命中；
- [ ] 报告（`outputs/segment_taxonomy_audit.md`）新增该列与「问题公司明细」条目；
- [ ] 对全样本输出一份「受影响公司清单 + 每个公司『产品标签 vs 行业标签』对照」，
      作为 Phase 2 判据宽窄的输入。

### 3.4 退出条件（gate 到 Phase 2）

- [ ] 已知受影响公司数量与典型形态（单桶粗口径 vs 单桶精确）清晰；
- [ ] 确认判据阈值（有效分项数 / top1 集中度）不会误伤「单桶但正确」的样本（至少中芯国际）。

---

## 4. Phase 2（方向一）：改选类判据 + 补单测（根治）

> 目标：把 `_pick_category` 从「固定产品优先」改为「粒度/信息量优先」，让工业富联得到
> `云计算`，同时不回归中芯国际这类单桶精确公司。

### 4.1 改动范围

仅 `alphabee/company_track/label.py`：
- `_pick_category`（第 52 行）改为「信息量优先」的选类；
- 新增纯函数辅助判定（如 `_effective_count` / `_top1_share`），放同文件常量/工具区；
- `derive_track_label` 在「回退行业口径」时写一条 `warnings`（留痕：产品口径粗退化）。

### 4.2 判据落地（行为描述）

`_pick_category` 的决策改为：

1. 对每个可选分类（先产品、后行业、再其他），计算「去 other 后有效分项数」与「top1 占比」；
2. **产品口径退化判定**：产品口径去 other 后 == 1 项，或 top1 占比 ≥ `_COARSE_SHARE`（默认 0.95）；
   且存在另一分类（行业）去 other 后 ≥ 2 项、top1 占比 < `_COARSE_SHARE` → 回退到该分类；
3. 否则维持「产品 → 行业」的原有优先顺序（保证中芯国际等不回归）。

> 推荐常量 `_COARSE_SHARE = 0.95`，与 Phase 1 判据对齐；阈值以 Phase 1 实测微调。
> 判据是纯函数、无 I/O、无随机源，符合仓库「确定性内核」约束。

### 4.3 单测（`tests/company_track/test_label.py` 新增）

至少四条：

- [ ] **正例（回退）**：产品单桶 `3C电子产品`(99.8%) + 行业 `云计算`/`通信设备`/`工业互联网`
      → `track_label == 云计算`，且 `warnings` 含「产品口径粗退化」；
- [ ] **反例（不误伤）**：产品单桶 `集成电路晶圆制造代工` + 行业单桶同名 → 仍用产品、标签不变；
- [ ] **反例（产品多桶不退化）**：产品口径 ≥ 2 项有区分度 → 仍走产品优先（维持现状行为）；
- [ ] **边界（top1 恰在阈值）**：构造 `_COARSE_SHARE` 临界样本，确认不等号方向稳定。

同步检查并更新依赖 track_label 的既有断言（`tests/orchestrator/test_resolve_driver_profile.py`
等，若其夹具使用了「按产品分类」单桶场景）。

### 4.4 验收标准

- [ ] 工业富联 601138：`build_company_track` 产出 `track_label=云计算`，`override_basis` 引用
      66.75% / yoy +88.7% 等真实数值；
- [ ] 中芯国际 688981：标签不变（不回归）；
- [ ] `tests/company_track/test_label.py` 全绿，新增四条通过；
- [ ] 全量单测 `poetry run pytest` 无回归。

### 4.5 连锁影响（Phase 2 必须一并处理）

1. **缓存失效重算**：`research_cache_key` 输出从 `…|3C电子产品|1` 变为 `…|云计算|1`，
   601138 已缓存的研究全部失效，下一次分析触发一次 LLM 重跑。**预期内、可接受**，
   需在改动说明标注。
2. **playbook 路由**：`context_router` 用 `track_label`（权重 3）路由；当前仅
   `generic_fundamental / mining_services / hog_cycle` 三个 playbook，无 AI 服务器框架，
   短期无感，修好后是正向收益（将来加「AI 服务器/云计算」框架能正确命中）。
3. **历史 task_record**：旧 `company_track_label` 是历史快照，与新标签不一致但无需处理。
4. **审计脚本对照**：Phase 1 的 `COARSE_PRODUCT_BUCKET` 输出应与 Phase 2 修复后的
   生产标签一致（可作为回归校验）。

---

## 5. Phase 3（方向三）：override 人工兜底

> 目标：对 Phase 2 规则修好后仍判不对、或需人工指定的个案，提供确定性、可审计的覆盖。

### 5.1 改动范围

`alphabee/company_track/track.py`（B3 override 机制已预留 `track_label` 修正字段）：
- 新增一张静态覆盖表（建议 `alphabee/company_track/overrides.yaml` 或 data 目录下同构文件），
  结构 `{symbol: {track_label, basis, effective_from}}`；
- `build_company_track` 组装前查表：命中则 `track_label` 取覆盖值、`override_basis` 记
  「人工覆盖：<basis>」、`track_method="override"` 留痕，未命中行为完全不变。

### 5.2 验收标准

- [ ] 覆盖表可读、可单测（命中/未命中/格式损坏三条路径）；
- [ ] `track_method` 新增 `override` 取值且下游不因未知枚举崩溃（确认 `CompanyTrackArtifact`
      校验对 `track_method` 的处理）；
- [ ] 初始表**只填已确认个案**（工业富联 → `云计算`），不得批量填规则能判对的公司。

> 定位约束：override 是**长尾兜底**，不是主方案。每填一条都应问「规则为何判不对」，
> 若可归纳成通用判据，应回填 Phase 2 而不是继续扩充 override 表。

---

## 6. 回滚与风险

| 风险 | 触发 | 缓解 |
| :--- | :--- | :--- |
| 判据过宽误伤「单桶正确」公司 | Phase 2 回归中芯国际失败 | 反例单测卡住；`_COARSE_SHARE` 阈值可调 |
| 标签变化导致缓存重算成本 | Phase 2 上线 | 预期内；影响面由 Phase 1 量化，可分批灰度 |
| 审计脚本与生产判据漂移 | 两处判据手写不一致 | 把 Phase 2 的判据抽成 `label.py` 公共纯函数，审计脚本复用，而非各自维护 |
| override 表不可控膨胀 | 逐公司填表 | 见 §5.2 定位约束；`track_method=override` 可被审计观测 |

回滚粒度：Phase 1 纯脚本可随时回滚；Phase 2 只改 `label.py`，单文件 revert + 补测即可；
Phase 3 删除覆盖表条目即回退。

---

## 7. 测试计划汇总

| 层级 | 用例 | 归属 |
| :--- | :--- | :--- |
| 单元 | `derive_track_label` 选类判据（回退/不误伤/多桶/阈值边界） | `tests/company_track/test_label.py` |
| 单元 | `build_company_track` override 命中/未命中/格式损坏 | `tests/company_track/test_track.py` |
| 审计 | `COARSE_PRODUCT_BUCKET` 命中/不误报 | `scripts/segment_taxonomy_audit.py`（跑样本） |
| 回归 | 依赖 track_label 的既有断言 | `tests/orchestrator/`、`tests/domain_context/` |
| 端到端（可选） | 工业富联 601138 完整 run 后 `company_track_label == 云计算` | 手工 golden |

---

## 8. 开放问题

1. `_COARSE_SHARE` 阈值（0.95）是否足够稳健？——由 Phase 1 全样本实测定稿。
2. 「信息量优先」是否要扩展到「地区分类之外的其他分类」（如「按业务/按模式」）？
   当前仅产品 vs 行业，先做最小闭环。
3. `track_method` 新增 `override` 是否会触发 `CompanyTrackArtifact` 的字段校验或下游分支？
   需在 Phase 3 实施前核对 `contracts.py`。
4. 缓存重算是否要提供「按 symbol 手动失效」的运维入口？——本次不做，仅记录影响。
