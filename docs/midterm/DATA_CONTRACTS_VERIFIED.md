# 数据接口契约核实记录（researcher 冒烟测试）

> 本文件由数据契约研究员维护，只记录**真实冒烟测试**核实过的外部接口入参/出参，
> 供 `adapters/`、`collectors/` 复用。原则：接口契约以官方文档 + 实测为准，不凭接口名猜。
> 每条契约含 `provider / api / availability / inputs / outputs / verified_at`。

---

## 1. 东方财富研报列表接口（consensus 主数据源）

> 该接口是 consensus 的**默认**数据源（`midterm.consensus_source: report_list`）；备选的东财 F10「盈利预测」接口见 §5（`f10_forecast` / `auto`），两者字段口径**不可混比**（§5「与研报列表路径的口径差异」）。

```text
provider: eastmoney
api: GET https://reportapi.eastmoney.com/report/list   （JSONP，回调名为 cb 参数）
availability: ok（免费，无需 token，HTTP 200）
verified_at: 2026-09-06
```

### inputs（实测确认）

| 参数 | 格式 | 说明 |
|------|------|------|
| `cb` | str | JSONP 回调名，任意标识符（如 `datatable`） |
| `pageNo` | int | 页码，从 1 起 |
| `pageSize` | int | 每页条数（实测 300 可用；翻页需用 TotalPage 判断） |
| `code` | str | 6 位股票代码（如 `300750`），`*` 表示全市场 |
| `industryCode` | str | 行业代码（东财），`*` 表示全部 |
| `beginTime` / `endTime` | str | `YYYY-MM-DD`，研报发布日期范围 |
| `qType` | int | 0=全部 / 1=仅有研报 / 2=仅无研报 |
| `_` | int | 时间戳防缓存（毫秒） |

### outputs（顶层）

```text
hits / size / data[] / TotalPage / pageNo / currentYear
```

### outputs（data[] 每条，实测打印 columns + head）

consensus 相关字段（覆盖率为 300750/600519/601318 三只实测样本）：

| 原始字段 | 含义 | 类型 | 覆盖率（实测） |
|----------|------|------|---------------|
| `predictThisYearEps` | FY1 每股收益预测 | str→float | 94%~98%（冷门股 36/41） |
| `predictNextYearEps` | FY2 每股收益预测 | str→float | 64%~98% |
| `predictNextTwoYearEps` | FY3 每股收益预测 | str→float | 24%~32%（冷门股 14/41） |
| `predictThisYearPe` / `predictNextYearPe` / `predictNextTwoYearPe` | 对应预测 PE | str→float | 与 EPS 同源，覆盖接近 100% |
| `predictLastYearEps` / `actualLastYearEps` / `actualLastTwoYearEps` | 去年/实际 EPS | str→float | 极少填充（1/39） |
| `indvAimPriceT` | 目标价（含评级价） | str→float | **低覆盖**：300750 29/100、600519 14/100、601318 4/41（需降级） |
| `indvAimPriceL` | 目标价下限 | str→float | 与 `indvAimPriceT` 同步 |
| `emRatingCode` / `emRatingName` / `emRatingValue` | 东财评级码/名/数值 | str/str/str | 100% |
| `lastEmRatingCode` / `lastEmRatingName` / `lastEmRatingValue` | **同一机构上一评级** | str/str/str | 100% |
| `ratingChange` | 东财分类码 | str/int | **非恒定**（600519 出现 1/2/3），语义不明，**不能当上/下调方向** |
| `sRatingCode` / `sRatingName` | 来源系统标准评级（如 优于大市/买进(Buy)/推荐） | str | 100% |
| `orgCode` / `orgName` / `orgSName` | 机构码/全称/简称 | str | 100% |
| `researcher` | 研究员（`,` 分隔） | str | 100% |
| `publishDate` | 发布日期（`YYYY-MM-DD HH:MM:SS.000`） | str | 100% |
| `infoCode` / `encodeUrl` | 详情/PDF 标识 | str | 100% |
| `indvInduCode` / `indvInduName` | 东财行业码/名（如 `1033`/电池） | str | 100% |
| `attachPages` / `attachSize` | 页数/附件大小 | int | 100% |

### ⚠️ 关键契约修正（与设计文档不一致）

1. **`emRatingValue` 编码方向与 `MIDTERM_FACTOR_DATA_DESIGN.md` §3.4 假设相反**：
   设计文档假设 `买入1/增持2/中性3/减持4/卖出5`（数值越小越看多）。
   实测编码为：**持有=1、增持=2、买入=3**（数值越大越看多）：
   ```text
   emRatingCode=005 → 持有   → emRatingValue=1
   emRatingCode=006 → 增持   → emRatingValue=2
   emRatingCode=007 → 买入   → emRatingValue=3
   ```
   `rating_mean` 聚合若直接用 `emRatingValue`，是"越大越看多"方向；不要照搬文档的
   "买入1"假设（否则方向反了）。

2. **评级上/下调方向不要读 `ratingChange`**：该字段非恒定（不同股票出现 `''/1/2/3`），
   是东财分类码。正确做法：直接用同一记录内的 `emRatingValue` vs `lastEmRatingValue`
   差分（`lastEmRatingValue` 是"该机构上一评级"，无需跨研报按 `(机构,股票)` 分组再 diff）。

3. **目标价覆盖率低**：`indvAimPriceT` 三只样本覆盖 4/41 ~ 29/100，落地 `target_price`
   时需显式 `consensus_missing: target_price` 降级。

### 字段丢弃现状（links.py）

- `alphabee/tools/eastmoney.py::get_eastmoney_report_list` → 返回 `EastmoneyHelper.process_data`
  处理后的**全量字段**（EPS/目标价/评级/lastEmRating 等都在，字段名保留原始名 + `industry_final_*`/`raw_json`）。
- `alphabee/financial_report/links.py::get_research_report_links` → 只透出
  `{title, date, org, researcher, rating, download_url, info_code, encode_url, pages}`，
  **丢弃** `predictThisYearEps/predictNextYearEps/predictNextTwoYearEps/predict*Pe/
  indvAimPriceT/L/emRatingValue/lastEmRating*/sRating*/indvIndu*`。
- 结论：consensus 聚合应直接消费 `get_eastmoney_report_list`（全量字段），
  不要走 `get_research_report_links`（它只服务 PDF 下载链接，字段被裁剪）。

---

## 2. AkShare `stock_hold_num_cninfo`（巨潮股东户数，crowding C4 筹码）

```text
provider: akshare（巨潮资讯 webapi.cninfo.com.cn）
api: stock_hold_num_cninfo(date="YYYYMMDD")
availability: ok（免费，无需 token）
verified_at: 2026-09-06
```

### inputs（实测确认）

| 参数 | 格式 | 说明 |
|------|------|------|
| `date` | str `YYYYMMDD` | 全市场某日快照；**必须季度末披露日**（0331/0630/0930/1231）。传非季度末日期（如 `20240915`）会抛 `KeyError: 'records'`（不是返回空表），调用方需 try/except。 |

- 底层：`POST https://webapi.cninfo.com.cn/api/sysapi/s_p_sysapi1034`，参数 `rdate`（需 `Accept-Enckey` 加密头，akshare 内部已处理）。
- 文档说"从 20170331 开始"，实测 `20161231` 也有 2782 行——起始边界并非硬约束。

### outputs（实测 columns + head，5110 行 × 9 列）

```text
证券代码(str) 证券简称(str) 变动日期(date) 本期股东人数(float) 上期股东人数(float)
股东人数增幅(float,%) 本期人均持股数量(float) 上期人均持股数量(float) 人均持股数量增幅(float,%)
```

- `股东人数增幅` / `人均持股数量增幅` 已是百分比数值（如 `-1.79` 表示 -1.79%），无需再乘 100。
- `变动日期` 即报告期（如 `2024-09-30`）。
- 全市场快照（实测 2024-09-30 为 5110 只、2026-03-31 为 5251 只、2016-12-31 为 2782 只）。

---

## 3. AkShare `stock_hot_rank_em`（东财人气榜，crowding C3 关注）

```text
provider: akshare（东方财富）
api: stock_hot_rank_em()
availability: 部分可用（沙箱内 push2.eastmoney.com 不可达 → 函数整体抛 ProxyError；但底层榜单接口可达）
verified_at: 2026-09-06
```

### 函数签名与预期出参（来自 akshare 源码，沙箱无法跑通完整函数）

- 签名：`stock_hot_rank_em() -> pd.DataFrame`，**无参**，返回 top100。
- 预期列：`当前排名, 代码, 股票名称, 最新价, 涨跌额, 涨跌幅`。
- 内部两步：① `POST https://emappdata.eastmoney.com/stockrank/getAllCurrentList`（榜单），
  ② `GET https://push2.eastmoney.com/api/qt/ulist.np/get`（补 名称/最新价/涨跌幅）。

### 实测沙箱阻塞（重要，生产需确认代理）

- 步骤① `emappdata.eastmoney.com/stockrank/getAllCurrentList` **可达**（走代理 200）。
  实测返回 100 行，每行 `{sc, rk, rc, hisRc}`：
  - `sc` = `SZ000592` / `SH605577`（交易所前缀 + 6 位代码）
  - `rk` = 当前排名（1–100）
  - `rc` = 排名变化（快照里多为 0）
  - `hisRc` = 历史排名变化
  - 分页上限：仅 `pageNo=1&pageSize=100` 有效；`pageSize>100` 或 `pageNo>1` 均返回 0 行（即只取 top100）。
- 步骤② `push2.eastmoney.com` **沙箱内不可达**：代理 `192.168.50.228:7897` → ProxyError；
  直连 → ConnectionError（RemoteDisconnected）。因此完整 `stock_hot_rank_em()` 在沙箱抛异常。
  这与设计文档对 `stock_cyq_em` 的"沙箱 ProxyError"记录同源（push2 域名）。

### 对下游的建议

- 若 `hot_rank`（人气榜排名）只需"排名 + 代码"，可直接消费步骤①
  `emappdata.eastmoney.com/stockrank/getAllCurrentList`（沙箱已实测可达），
  再由 `sc`（`SZ000592` → `000592`）转 6 位代码；股票名称需另查（如 Tushare `stock_basic`）。
- 若需"名称/最新价/涨跌幅"，生产环境需先确认 push2.eastmoney.com 可达性，或换源补名称。

### 底层接口请求体与响应信封（engineer-e 复验 2026-09-06，HTTP 200）

```text
POST https://emappdata.eastmoney.com/stockrank/getAllCurrentList
Content-Type: application/json
请求体（JSON）:
  {
    "appId": "appId01",
    "globalId": "786e4c21-70dc-435a-93bb-38",
    "marketType": "",
    "pageNo": 1,
    "pageSize": 100
  }

响应信封（JSON，HTTP 200）:
  {
    "globalId": "786e4c21-70dc-435a-93bb-38",
    "message": "",
    "status": 0,
    "code": 0,
    "data": [ 100 行, 仅 top100 ],
    "stack": ""
  }
  data[] 每行: { "sc": "SZ000592", "rk": 1, "rc": 0, "hisRc": 0 }
    - sc    = 交易所前缀 + 6 位代码（SZ000592 / SH605577）
    - rk    = 当前排名（1–100）
    - rc    = 排名变化（快照多为 0）
    - hisRc = 历史排名变化
```

> 采集器 ``alphabee/collectors/crowding/hot_rank.py`` 的请求常量（``EMAPP_HOT_RANK_URL`` /
> ``EMAPP_HOT_RANK_PAYLOAD``）即以上述请求体为准；只消费 ``data[].sc/rk``，其余字段忽略。

---

## 4. Tushare `penalty`（违规处罚）——接口不存在（设计文档假设有误）

```text
provider: tushare
api: penalty
availability: 不存在（实测 pro.penalty(...) → "请指定正确的接口名"；官方目录 tushare.pro/document/2 无此接口）
verified_at: 2026-09-06
```

### 实测结论

1. `pro.penalty(ts_code=...)` / `pro.query("penalty")` 均返回 **`Exception 请指定正确的接口名`**，
   即 tushare pro 服务端**不存在** `penalty` 接口（非积分不足——积分不足会返回"您没有该接口访问权限"）。
2. 对照官方接口目录（[tushare.pro/document/2](https://tushare.pro/document/2)）逐项核对，
   参考数据/大模型语料下均**无**"违规处罚"接口。
3. 因此 `MIDTERM_FACTOR_DATA_DESIGN.md` §7.3（`penalty` 违规处罚）、§10 速查表、ROADMAP 落地清单
   中"Tushare `penalty`"的假设不成立，**R 因子不能按原方案接入**，需改选替代源。

### 可用的替代接口（R 因子"违规/监管/审计风险"方向，实测）

| 接口 | doc_id | 说明 | 当前 token 实测 |
|------|--------|------|----------------|
| `fina_audit`（财务审计意见） | 80 | **可用**。入参 `ts_code` **必填**；输出 `ts_code/ann_date/end_date/audit_result/audit_fees/audit_agency/audit_sign`。`audit_result` 值如 `标准无保留意见` 等，非标准无保留意见可作审计风险信号。 | ✅ 有权限，实测 26 行 |
| `stk_alert`（交易所重点提示证券） | 453 | 监管重点提示。入参 `ts_code/trade_date/start_date/end_date`（均 N）；输出 `ts_code/name/start_date/end_date/type`。**需 6000 积分**。 | ❌ 无权限（积分不足） |
| `stk_shock`（个股异常波动） | 451 | 异常波动。入参 `ts_code/trade_date/start_date/end_date`；输出 `ts_code/trade_date/name/trade_market/reason/period`。**需 6000 积分**。 | ❌ 无权限（积分不足） |
| `anns_d` / 上市公司公告 | 176 | 公告语料（大模型语料），可关键词过滤"处罚/违规/立案"，但非结构化处罚字段。 | 未在本任务实测 |

### 对下游的建议

- R 因子"违规/处罚"落地优先用 **`fina_audit`（免费、已可用）** 作"审计异常"直接字段；
  "交易所监管提示/异常波动"需 6000 积分（`stk_alert`/`stk_shock`），落地前需确认积分；
  "违规/立案"结构化数据需换源（AkShare/东财公告，或 `anns_d` 公告语料过滤）。

---

## 5. 东方财富 F10「盈利预测」接口（consensus 备选数据源）

> 完整实测报告（含逐区块原始 JSON 摘录、延迟、复现命令）：`docs/implementation/midterm/E_ECOSYSTEM_SMOKE_REPORT.md`；
> 实现侧登记：`alphabee/collectors/consensus/emforecast.py` 模块 docstring。数据源切换设计：`docs/implementation/midterm/E_ECOSYSTEM_FIX_DESIGN.md` §5。

```text
provider: eastmoney（PC_HSF10 盈利预测页）
api: GET https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=<PREFIX+6位>
availability: ok（免费，无需 token / UA / Referer / Cookie；CDN 缓存 120 s）
verified_at: 2026-10-19
```

### inputs（实测确认）

| 参数 | 格式 | 说明 |
|------|------|------|
| `code` | str | **必须「交易所前缀 + 6 位」**：`SH600519` / `SZ000333` / `BJ920002`；前缀大小写不敏感。**无前缀、`.SH` 后缀、前缀与代码段错配、前导空格** 一律返回 `{"status": -1, "message": "股票代码不合法"}`（**HTTP 仍 200**）。北交所仅 `920xxx` 可用，`43xxxx` / `83xxxx` 被拒 ⇒ 实现侧不发请求、直接回落 |

- 唯一业务参数就是 `code`；`pageNum` / `page` / `ps` / `pageSize` / `type` **全部被忽略**（响应字节数与 md5 完全相同）⇒ **本接口无分页**。
- 请求头：**不校验 UA / Referer / Cookie / Origin**（显式空 UA 仍 200，贵州茅台样本 37,686 B）。
- 备用域名 `emweb.eastmoney.com` 与主域名**字节级一致**（md5 相同）。
- **失败判定**：非法代码返回 HTTP 200 + `{"status": -1}`，`raise_for_status()` 不会触发 ⇒ fail-open 必须**解析 JSON 后判键**（缺 `pjtj`/`jgyc`/`yctj_list` 即视为失败），不得只看状态码。

### outputs（顶层，固定 5 个区块，全部为 list，无 `data` 包装）

| 区块 | 含义 | 行数特征 |
|------|------|----------|
| `pjtj` | 评级统计 | 固定 5 行（1/2/3/6 月、1 年） |
| `jgyc` | 机构预测 | `0` 或 `1 + N`（N ≤ 30）；第 0 行为「近六月平均」聚合行（`ORG_CODE=00000000`） |
| `yctj_chart` | 预测统计（图表版） | 0~4 行（有 `*_RATIO`/`PE`，**无 `*_COUNT`**） |
| `yctj_list` | 预测统计（表格版） | 0~6 行（3 实际 + 3 预测），**一致预期均值 + 各指标机构数** |
| `ycmx` | 预测明细 | 0~30 行（**被截断为「近 6 个月、最多 30 条」**，短于 `EPS_COUNT`） |

### 字段映射：东财字段 → 既有 canonical（实测，对照设计 §5.1）

| canonical | 东财字段（实测） | 取数规则 | 备注 |
|---|---|---|---|
| `eps_fy1` | `yctj_list[].EPS` | `YEAR_MARK == "E"` 的**第 1 行** | 实测该行为**当前财年**（2026-10-19 时 `YEAR=2026`） |
| `eps_fy2` | `yctj_list[].EPS` | 同上第 2 行 | 实测 `YEAR=2027` |
| `eps_fy3` | `yctj_list[].EPS` | 同上第 3 行（可缺） | 实测 `YEAR=2028`，覆盖率略低于 FY1/FY2 |
| `coverage_count` | `yctj_list[].EPS_COUNT` | 取与 `eps_fy1` **同一行** | **机构家数**（非研报数）；各指标家数互不相等，固定用 `EPS_COUNT` |
| `as_of_date` | `jgyc[0].PUBLISH_DATE` | 取日期部分（`YYYY-MM-DD`） | 聚合行（`ORG_CODE=00000000`/`ORG_NAME_ABBR=近六月平均`）恒为**数据刷新日**（≠ 研报日）；`jgyc` 为空 ⇒ 回落本地抓取日 |
| （快照审计用，非 canonical） | `yctj_list[].EPS_LASTMONTHS` / `EPS_LASTMONTHS_COUNT` | 随快照留痕 | 上一期同口径均值与家数（家数会变，实测 46→44）；**当前不参与 revision 计算**（语义待对拍） |
| （可选，未落 canonical） | `yctj_list[].PARENT_NETPROFIT` / `TOTAL_OPERATE_INCOME` / `ROE` | 与 EPS 同行 | 设计 §5.1 标注可选，实测有值；本设计不消费 |

### 实测**不可得**的字段（不得凭接口名假设）

| canonical | 实测结论 | 实现侧结论 |
|---|---|---|
| `target_price` | **接口完全不存在**（25 区块键名全量扫描 + 原文 grep `target`/`目标价` 双证 0 命中；页面仅 4 个 Tab，无目标价 Tab） | 恒 `None` + `consensus_missing`；目标价只来自 §1 研报列表路径 |
| `rating_mean` | `pjtj[]` 有 5 分制均分（`COMPRE_RATING_NUM`，实测 4.75~4.84），与 §1 研报路径 `emRatingValue` 的 **3 分制**（持有1/增持2/买入3）**不可比** | 保持 `None`（不污染与 report_list 帧的可比性） |
| `rating_upgrade_1m` / `rating_downgrade_1m` / `revision_breadth` | `pjtj`/`ycmx` **无上期评级字段**（`ycmx[].RATING` 只有当前值） | 恒 `None` + `consensus_missing` |
| EPS 最高/最低 | **接口不提供**；只能从 `ycmx[]` 自算，而明细被截断为最多 30 条（贵州茅台 `EPS_COUNT=44`、明细 30）⇒ 自算必偏窄 | **不落 max/min**（本设计无对应 canonical 字段） |
| `eps_fy1_revision_3m` 等窗口类 | 接口本身**无历史时序**（只给当前快照），且无 3 个月前值 | 由 `data/consensus_snapshots/<纯6位代码>.jsonl` append-only 快照序列纯规则自算；窗口（30/90 天）未满 ⇒ `None` |

### 空值形态（实测三类 + 一类错误）

| 形态 | 实测样本 | 判据 |
|---|---|---|
| **无机构覆盖** | `SH600112` / `SZ000995` / `SH603207` | `pjtj=[]`、`jgyc=[]`、`ycmx=[]`，且 `yctj_list` **仍返回 3 个实际年度但无 `YEAR_MARK=="E"` 行** |
| **次新** | `SH603202` | `pjtj` 有评级（仅「1年内」行有值）但 `jgyc=[]`、无 E 行 ⇒ 评级与一致预期是**两个独立区块**，不能由 `pjtj` 有值推断 `eps_fy1` 有值 |
| **退市/长期停牌类** | `SH600608` / `600145` / `000033` / `600087` | 与「无覆盖」同形（实际年度陈旧） |
| **非法代码**（唯一返回 `status` 的形态） | 无前缀 / 带后缀 / 前缀错配 / 前导空格 / 未发行代码 | HTTP 200 + `{"status": -1, "message": "股票代码不合法"}` |

- **存在性判据**：`jgyc` 为空 **且** 无 `YEAR_MARK == "E"` 行 ⇒ 无可用快照。**绝不能**用「`yctj_list` 非空」判有覆盖（无覆盖时仍有 3 个实际年度行），也不能只看 `EPS is None`（无覆盖时根本不给预测年度行）。
- 上述形态在实现侧统一视为「无可用快照」⇒ `fetch_profit_forecast` 返回 `{}`、`build_f10_consensus` 返回 `None`，由 provider 链回落 `report_list` 并在 `ConsensusOutput.warnings` 留痕。

### 与研报列表路径（§1）的口径差异（登记条款）

- `coverage_count`：§1 是**研报数**（`_coverage_count`），§5 是**机构家数**（`EPS_COUNT`）⇒ **两种口径的帧不可直接混比**（对比实验只在新源启用后的帧内进行；P0-6 的 `<3` 护栏判据不变）。
- `as_of_date`：§1 是**聚合基准日**（`aggregate_consensus` 缺省用今天，可显式传 `as_of_date` 覆盖），§5 取服务端**数据刷新日**（`jgyc` 聚合行 `PUBLISH_DATE`，缺则回落本地抓取日）。
- `target_price` / `rating_*` / `revision_breadth`：§1 稀疏但有值，§5 **恒 `None`**（见「实测不可得的字段」）。

### schema-steward 条目（备查）

> **本设计无新 canonical 字段**：13 个 consensus 字段（`alphabee/schemas/consensus.yaml`：`eps_fy1`/`eps_fy2`/`eps_fy3`/`target_price`/`rating_mean`/`coverage_count`/`eps_fy1_revision_1m`/`eps_fy1_revision_3m`/`eps_fy2_revision_1m`/`revision_breadth`/`revision_acceleration`/`rating_upgrade_1m`/`rating_downgrade_1m`）全部**复用既有定义**，本接口只做「东财字段 → 既有 canonical」的映射（上表），**不需要注册任何新字段**；`schemas/INDEX.yaml` / `schemas/consensus.yaml` 均未改动。
> 数据源字段名的消费面被限制在 `alphabee/collectors/consensus/emforecast.py`（source-specific collector，输出一律 canonical），下游按 canonical 名读取，不感知东财键名。

---

## 6. Tushare 财务四表：重复行 / 修订标记 / 公司类型 / 营收口径（2026-11-02 实测）

> 设计依据：`docs/implementation/midterm/MIDTERM_F_DATA_GOVERNANCE_DESIGN.md` §1.3（实测证据）/ §5.2（去重算法）/ §11.2（公司类型）；
> 采集侧落点：`alphabee/agents/facts/tools/financial_fact.py`；映射：`alphabee/adapters/tushare/financial_mapping.yaml`。
> **2026-11-04 复测**：使用本机 `.env` 的 `TUSHARE_TOKEN` 逐项复跑，下列事实与 2026-11-02 实测一致（复测记录见本节末尾）。本接口契约只增加既有接口的返回列，**不新增外部数据源**。

### 接口与字段（实测确认）

| 接口 | 本次新增读取的列 | 实测说明 |
|---|---|---|
| `income` | `ann_date` / `report_type` / `update_flag` / `comp_type` / `revenue` | 同报告期可多行（初始/修订/更正）；`comp_type` 只在 `income` 返回 |
| `fina_indicator` | `ann_date` / `update_flag` / `dt_netprofit_yoy` | 同报告期可多行；`update_flag=0` 行常为 NaN（扣非列整列丢失风险） |
| `cashflow` | `ann_date` / `report_type` / `update_flag` | 列可用；`ann_date` 作为 provenance 保留 |
| `balancesheet` | `ann_date` / `report_type` / `update_flag` | 列可用；`ann_date` 作为 provenance 保留 |

- 四表均有 `ann_date`（公告日，`YYYYMMDD`）；`income` / `balancesheet` / `cashflow` 另有 `report_type`（实测样本均为 `1`=合并报表）与 `update_flag`（`0`/`1`）。
- 同报告期重复行是**常态**（修订、更正公告、快报），不能按返回行序取首条（旧 `drop_duplicates(keep="first")` 的问题）。

### 实测样本一：688981.SH 2024 年报（`income`，同 `end_date=20241231` 三行）

| `ann_date` | `update_flag` | `report_type` | `comp_type` | `total_revenue`（元） |
|---|---|---|---|---|
| 20250212 | 0 | 1 | 1 | 57,745,538,740.4 |
| 20250212 | 1 | 1 | 1 | 57,795,570,000.0 |
| 20250328 | 1 | 1 | 1 | 57,795,570,000.0 |

- **去重预期**：合并报表优先 → 同报告期按 `ann_date`、`update_flag` 降序 → 取 20250328 / flag 1 的 **57,795,570,000.0**（最终修订值）；旧行序实现会取到 20250212 / flag 0 的 **57,745,538,740.4**（初值）。
- 该样本的 `total_revenue` 与 `revenue` 数值相同（一般工业公司两口径可相等）。

### 实测样本二：000002.SZ 2026Q3（`fina_indicator`，同 `end_date=20260930` 两行）

| `ann_date` | `update_flag` | `profit_dedt`（元） | `dt_netprofit_yoy`（%） | `grossprofit_margin`（%） |
|---|---|---|---|---|
| 20261030 | 1 | −21,537,520,000 | 18.68 | 6.6382 |
| 20261030 | 0 | NaN | NaN | 6.6382 |

- **去重预期**：`update_flag=1` 行有扣非值、`0` 行 NaN；按 `update_flag` 降序后取到非空扣非行。若行序反转且不加 `prefer_nonnull` 稳定排序键，`profit_dedt` / `dt_netprofit_yoy` 会整列丢失。
- `dt_netprofit_yoy` 与 `profit_dedt` 同源：扣非归母净利累计同比，映射为 canonical `net_profit_dedt_yoy`。

### 实测样本三：金融股 `comp_type`（`income`）

| 标的 | 公司 | `comp_type`（实测集合） | canonical `company_type` / 判定 |
|---|---|---|---|
| 601318.SH | 中国平安 | 3 | 保险 ⇒ `is_financial=True` |
| 600036.SH | 招商银行 | 2 | 银行 ⇒ `is_financial=True` |
| 600030.SH | 中信证券 | 4 | 证券 ⇒ `is_financial=True` |

- 一般工业样本（688981 / 600519 等）实测 `comp_type=1`。`comp_type` 缺失 ⇒ `is_financial=False`（fail-open）并登记 `missing_facts=company_type`。

### 实测样本四：`income.total_revenue` vs `income.revenue`（600519.SH，2026 年）

| `end_date` | `total_revenue`（营业总收入，元） | `revenue`（营业收入，元） | 差额 |
|---|---|---|---|
| 20260331 | 54,702,912,385.23 | 53,909,252,220.51 | 793,660,164.72 |
| 20260630 | 92,278,072,083.21 | 90,703,260,964.48 | 1,574,811,118.73 |
| 20260930 | 133,283,652,508.95 | 130,970,743,649.04 | 2,312,908,859.91 |

- 两列**不是同一口径**：`total_revenue` = 营业总收入（既有 canonical `revenue`，语义不变），`revenue` = 营业收入（新增 canonical `operating_revenue`）。同一接口必须映射为两个不同 canonical 列，不能重名。
- 边际层 `q_revenue_yoy` / `q_gross_margin` 用 `operating_revenue`（与 `or_yoy`、`grossprofit_margin` 的分母同口径）；`operating_revenue` 缺失时才回落 `total_revenue` 并记 `marginal_revenue_basis="total_revenue"`。

### 实测样本五：600760.SH 最新预告（`forecast`）

| `ann_date` | `end_date` | `type` | `p_change_min` | `p_change_max` | `update_flag` |
|---|---|---|---|---|---|
| 20260715 | 20260630 | 预减 | −58.23 | −58.23 | 0 |

- 最新预告的报告期是 **20260630（H1）**，而 2026-11-01 帧的最新财报期是 **20260930（Q3）** ⇒ 旧逻辑会把 H1 预告与 Q3 财报相比并误产 beat（P0-1 的实测动因）。同报告期才允许比较。

### 去重算法边界（实现口径，P0-2）

1. `report_type` 有 `1` ⇒ 只保留合并报表；没有 `1` ⇒ 保留全部（fail-open，不因过滤丢数据）。
2. 同报告期按 `ann_date`（归一 `YYYYMMDD`，缺失/非法回落 `period`）与 `update_flag` 降序；`ann_date` 缺失/非法时回落 `period` 排序，避免 `"nan"` 字符串排到数字前。
3. `fina_indicator` 追加「非空字段计数降序」稳定排序键（`net_profit_dedt` / `gross_margin`），同 `period/ann_date/update_flag` 并列时优先有值行。
4. 同报告期取首条（= 最新公告 + 最新修订）；`report_type` / `update_flag` 去重后丢弃，`ann_date` 与 `company_type` 保留（provenance）。
5. `ref_dates` 仍为报告期降序（最新一期在前）；空 DataFrame 原样返回（不制造列）。

### 2026-11-04 复测记录（本机 `.env` token）

- `688981.SH` 2024 年报仍为三行（20250212 flag 0 / 20250212 flag 1 / 20250328 flag 1），数值与 2026-11-02 一致。
- `000002.SZ` 2026Q3 `fina_indicator` 仍为两行（`ann_date=20261030`）：flag 1 行有 `profit_dedt=-21,537,520,000`、`dt_netprofit_yoy=18.68`；flag 0 行两列 NaN。
- `comp_type` 仍为 601318=3 / 600036=2 / 600030=4；一般工业样本为 1。
- `cashflow` / `balancesheet` 的 `ann_date` / `report_type` / `update_flag` 三列可用（600519 2026Q3 实测）。
- `600519.SH` 的 `total_revenue` 仍不等于 `revenue`（2026Q3：133,283,652,508.95 vs 130,970,743,649.04）。
- `600760.SH` 最新预告 `end_date=20260630`（`ann_date=20260715`，`p_change_min/max=-58.23`），与设计 §1.3 一致。

> **结论**：设计 §1.3 的四项契约事实（`update_flag` 重复行、`comp_type` 取值、`dt_netprofit_yoy`、`income.revenue` vs `total_revenue`）在 2026-11-02 与 2026-11-04 两次实测中一致；实现侧按上述算法消费，不新增外部接口。
