---
name: data-source-contract
description: 当编写或修改 Tushare / AkShare / Baostock 外部数据接口调用代码、新增数据获取逻辑、设计 adapter 或行业分类匹配时使用。要求先冒烟测试接口是否可用，通过实测明确入参与出参，核对数据分类的来源体系（证监会/申万/东财/同花顺），并统一来源避免跨体系匹配不上的情况。适用于 A 股行情、财务、行业分类、估值等数据获取代码开发。
---

# Skill: 外部数据接口契约校验（tushare / akshare / baostock）

## 目标

在写外部数据接口调用代码之前，先验证接口是否可用，通过真实调用明确入参、出参，核对数据分类的来源体系，并在跨源使用时统一来源，避免"接口名猜对了、参数用错了、返回全空、跨体系匹配不上"这类问题。

核心原则一句话：

> **技能/文档只负责指路，接口契约以官方文档 + 真实冒烟测试为准。**

## 什么时候使用本 Skill

- 写或改 `tushare` / `akshare` / `baostock` 的调用代码
- 新增数据获取、adapter 字段映射、行业/板块分类匹配逻辑
- 遇到接口返回空表、字段缺失、匹配不上、权限报错
- review 他人写的外部数据获取代码

## 四条铁律

### 1. 先测可用性，再写代码

动手前先确认环境与权限，不要假设接口一定能调通：

- Token / API Key 是否有效（失效会直接返回"您的token不对"或空表）
- 接口所需积分 / 权限是否达到（如 tushare `sw_daily` 需 5000 积分、`index_member_all` 需 2000 积分）
- 接口是否存在（`没有接口` 报错）
- 空返回的语义区分：非交易日 / 无权限 / 参数错误 / 标的不存在

### 2. 入参出参以实测为准，不要凭接口名猜

接口名接近 ≠ 参数相同。必须**冒烟打印**确认：

- 实际可用参数（官方文档列出的才传）
- 返回列名与列含义（打印 `columns` + `head`）
- 代码/日期格式（如 `801120` vs `801120.SI`）

反例：凭接口名猜测 `index_member_all(src="SW2021")` —— 该接口根本没有 `src` 参数，
返回空；输出列是 `l1_code/l1_name/.../l3_code/l3_name`，不是臆想的 `index_code/con_code`。

### 3. 核对数据分类的来源体系

不同数据源/接口口中的"行业"可能来自完全不同的分类体系，命名与颗粒度都不同：

| 体系 | 来源 | tushare | akshare | baostock |
|------|------|---------|---------|----------|
| 证监会行业分类 | 行政口径 | `stock_basic.industry` | — | `query_stock_industry` |
| 申万 2021 | 指数口径 | `index_classify` / `index_member_all` | `index_hist_sw` / `sw_index_first/second/third_info` / `index_analysis_daily_sw` / `sw_index_spot` | — |
| 东方财富行业 | 板块口径 | — | `stock_board_industry_name_em` | — |
| 同花顺行业/概念 | 概念口径 | `ths_index` / `ths_member` | `stock_board_industry_index_ths` | — |

典型错配：证监会叫"电气设备"，申万 L1 叫"电力设备"；申万 L3 叫"线缆部件及其他"。
用证监会的行业名去匹配申万名，注定大面积失败，只有恰好同名的（半导体、白酒）才碰巧命中。

### 4. 统一来源，避免匹配不上

跨接口/跨源做匹配时，必须先确认两边是不是**同一套分类体系**：

- 个股 → 申万归属：用 `index_member_all(ts_code=...)` 直接拿权威 L1/L2/L3，**不要**拿
  `stock_basic.industry`（证监会名）去猜申万名
- 行情/估值：全程用同一套 sw_code（`index_classify`/`index_member_all` 的代码
  与 `sw_daily` 的 `ts_code` 是同源同码）
- 名称匹配只在同体系内做；跨体系必须先映射或改用权威归属
- 在 AlphaBee 里：外部字段名只允许出现在 adapter/mapping 层，业务代码只用 canonical 字段

## 标准流程（冒烟测试工作流）

写调用代码前，对每个新接口走一遍：

1. **环境检查**：token/包/权限是否就绪
2. **最小入参调用**：用一个已知标的或最小参数调一次
3. **打印契约**：`print(df.columns)` + `print(df.head())`，核对列名、格式、行数
4. **边界验证**：空结果 / 权限不足 / 代码格式（带不带后缀）各自什么表现
5. **记录契约**：把核实过的入参出参写进代码注释或文档，供后续复用
6. **决定使用**：可用则写代码；不可用则显式降级（换源/回退），不留死代码

模板脚本见 `scripts/smoke_test.py`。

## 已核实的接口契约（2026-08 实测/官方文档核对）

> 这些契约可能随上游变更，再次使用前建议重新冒烟。

**tushare**
- `stock_basic`：无 `sector` 字段；`industry` 是证监会口径名（如"电气设备"）
- `index_classify(level=L1/L2/L3, src="SW2021")`：出参 `index_code/industry_name/...`，
  adapter 重命名后为 `sw_code/industry_name`
- `index_member_all`：**无 `src` 参数**，入参 `l1_code/l2_code/l3_code/ts_code/is_new`；
  单只查询 `index_member_all(ts_code="000001.SZ")` 直接返回该股 L1/L2/L3 归属；
  出参 `l1_code/l1_name/l2_code/l2_name/l3_code/l3_name/ts_code/in_date/out_date/is_new`；
  需 2000 积分，单次最大 2000 行
- `sw_daily`：申万行业日线，需 5000 积分；出参含 `pe/pb/pct_change`；
  代码支持 L1/L2/L3（`801xxx` / `850xxx` / `859xxx`）

**akshare**（免费，无需 token）
- `index_hist_sw(symbol="801120", period="day")`：申万宏源研究官网；代码 6 位（去 `.SI`）；
  出参 `代码/日期/收盘/开盘/最高/最低/成交量/成交额`，**无涨跌幅、无 PE/PB**；
  L1/L2/L3 都支持，但部分历史代码数据陈旧
- `sw_index_first/second/third_info()`：乐咕乐股，L1/L2/L3 行业列表含
  `静态市盈率/TTM(滚动)市盈率/市净率/静态股息率`；网页源**偶发加载失败**，需重试
- `index_analysis_daily_sw(symbol="一级行业", start_date, end_date)`：申万宏源研究指数分析，
  逐日含 `市盈率/市净率/涨跌幅`；`symbol` 仅支持 `市场表征/一级行业/二级行业/风格指数`，
  **无三级行业**；指数代码为 6 位

## 反例教训（本仓库实际踩过）

1. `index_member_all(src="SW2021")` → 无 `src` 参数，返回空
2. `stock_basic` 请求 `sector` 字段 → 该字段不存在，永远为空
3. 拿 `stock_basic.industry`（证监会名）匹配申万 `index_classify`（申万名）→
   "电气设备" vs "电力设备" 大面积匹配失败
4. 用错误代码（如 `801732.SI`）测行情 → 数据陈旧/为空，真实 L2 代码是 `801738.SI`

## 输出格式

完成接口核实后，优先输出结构化结论：

```text
接口核实:
  provider: tushare | akshare | baostock
  api: <接口名>
  availability: ok | 无权限(需X积分) | 不存在 | 空返回
  inputs: {参数: 格式/说明}
  outputs: [列名: 含义]
  classification_source: 证监会 | 申万2021 | 东财 | 同花顺 | 无分类
  verified_at: 2026-08

code_changes:
  - file: ...
    change: ...

unification_notes:
  - 个股申万归属统一走 index_member_all(ts_code=...)，不要用行业名跨体系匹配
  - 估值/行情统一用同一 sw_code
