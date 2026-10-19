# E 数据生态冒烟报告 — 东财 F10「盈利预测」接口

> **状态：冒烟实测完成（2026-10-19）。** 本文是设计文档 `E_ECOSYSTEM_FIX_DESIGN.md` §5（PR-B：东财 F10 盈利预测数据源）的**开工前置件**，遵循 `data-source-contract` skill 的四条铁律：先测可用性、入参出参以实测为准、核对分类体系、统一来源。
> **纪律声明**：下文所有字段名、类型、空值形态均来自**真实 HTTP 响应**（原始响应已逐字段摘录于 §5/§6），无任何按接口名或设计文档推测的成分；凡"未能实测"的项目在 §10 显式标注，不作补白。
> 实测环境：2026-10-19，curl 8.18.0 / Python 3.13.12，出口网络可直连 `emweb.securities.eastmoney.com`（HTTP 200，见 §1.5 延迟）。
> 边界：本文只登记数据源契约，不含任何实现代码；`alphabee/` 与 `tests/` 未改动。

---

## 0. 冒烟结论摘要（PR-B 可用性判定）

| 判定项 | 结论 |
|---|---|
| 接口可用性 | **ok** —— 5 个覆盖样本 + 10 个空值/次新/退市样本 + 一批非法代码反例，全部拿到 HTTP 200 JSON；无鉴权、无 token、无 Cookie |
| 入参 | 单一参数 `code`，**必须带交易所前缀**且 6 位代码，形如 `SH600519` / `SZ000333` / `BJ920002` |
| 出参 | 单次返回 5 个区块（`pjtj` / `jgyc` / `yctj_chart` / `yctj_list` / `ycmx`），**无分页** |
| 一致预期 EPS 均值 | **有** —— `yctj_list[].EPS`（实测 = 机构明细 EPS 的算术均值） |
| 机构数（coverage） | **有** —— `yctj_list[].EPS_COUNT`（int，家数，非研报数） |
| EPS 最高/最低 | **接口不提供** —— 需从 `ycmx[]` 明细自算，且明细最多 30 条、短于 `EPS_COUNT`，**自算结果偏窄** |
| 目标价 | **完全不存在** —— 全响应无任何 target/目标价字段（键名扫描 + 原文 grep 双证） |
| 评级分布 | **有** —— `pjtj[]` 按 5 个时间窗给出买入/增持/中性/减持/卖出的家数与 5 分制均分 |
| 空值形态 | 无覆盖 / 次新 / 退市：**HTTP 依然 200、`pjtj`/`jgyc`/`ycmx` 为空数组、无预测年度行**；仅非法代码返回 `{"status":-1}` |
| 请求头要求 | **无** —— UA / Referer / Cookie 全部不需要（带与不带响应字节一致） |
| 结论 | **PR-B 可以按本表落地**；`target_price` 与 EPS max/min 必须显式降级（见 §8.3、§10） |

---

## 1. 接口与请求契约

### 1.1 地址（两个域名返回字节级一致，md5 相同）

| 用途 | URL |
|---|---|
| 主用（设计 §5.1 指定） | `https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=<PREFIX+6位代码>` |
| 备用（公司新域名） | `https://emweb.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=<同上>` |

实测两者对 `SH600519` 返回 `37,686` 字节、`md5=229050ba1c04983269bc17fe2fe95bad`（完全一致）。

### 1.2 请求方法 / 参数

- 方法：`GET`；**只有一个业务参数** `code`。
- `http://` 与 `https://` 均可用（`http` 返回 200，无 301 跳转）。
- 附加任意查询参数（`&pageNum=2` / `&page=2` / `&ps=200` / `&pageSize=200` / `&type=web`）**全部被忽略**：五种写法的响应字节数与 md5 与不带参数**完全相同**（`37686` 字节）→ **本接口无分页参数**。

### 1.3 请求头要求（实测矩阵）

| 场景 | HTTP | 字节数 |
|---|---|---|
| 无任何自定义头（curl 默认 UA） | 200 | 37686 |
| `User-Agent: Mozilla/5.0 ... Chrome/130` | 200 | 37686 |
| `Referer: https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/Index?type=web&code=SH600519` | 200 | 37686 |
| `User-Agent:`（**显式置空**） | 200 | 37686 |

结论：**不校验 UA / Referer / Cookie / Origin**，无需伪装浏览器。

### 1.4 响应头（含缓存语义，影响"快照日"判断）

```text
HTTP/1.1 200 OK
Server: Tengine
Content-Type: application/json; charset=utf-8
Cache-Control: public, max-age=120
Last-Modified: Mon, 19 Oct 2026 11:25:55 GMT
X-Powered-By: ASP.NET
Strict-Transport-Security: max-age=31536000
```

- 编码：UTF-8 JSON（中文股票名/券商名直接以 UTF-8 返回，非 unicode 转义）。
- **CDN 缓存 120 秒**：同一 `code` 在 2 分钟内重复请求可能拿到同一份快照（实测连续 3 次 md5 相同）。
- 对"快照"语义的影响：接口本身**无历史时序**，只给当前快照；若 PR-B 要落 `data/consensus_snapshots/<symbol>.jsonl`，同一交易日内多次调用需按 §5.2 的同日幂等处理。

### 1.5 延迟与稳定性

| 项 | 实测 |
|---|---|
| 单次响应耗时 | 0.05 s ~ 0.15 s（5 个样本） |
| 响应体大小 | 7,872 B（次新/弱覆盖）~ 37,686 B（贵州茅台，44 家机构） |
| 连续 3 次一致性 | md5 完全相同 |
| 失败形态 | 本次冒烟期间**未出现**网络失败/限流，故"fail-open 返回 `{}`"只能由 PR-B 的 try/except 保证，无法用真实失败样本验证（见 §10） |

---

## 2. `code` 入参格式（实测枚举）

| 传入值 | 结果 | 说明 |
|---|---|---|
| `SH600519` | ✅ 200，`SECUCODE=600519.SH` | 正确格式：**交易所前缀大写 + 6 位代码** |
| `sh600519` | ✅ 200，同上 | **前缀大小写不敏感** |
| `SZ000333` | ✅ 200，`SECUCODE=000333.SZ` | 深市 |
| `SZ301489` | ✅ 200，`SECUCODE=301489.SZ` | 创业板 |
| `SH688981` | ✅ 200，`SECUCODE=688981.SH` | 科创板 |
| `BJ920002` | ✅ 200，`SECUCODE=920002.BJ` | **北交所仅新代码段 `920xxx` 可用** |
| `BJ430047` / `BJ832735` / `BJ835185` / `BJ830799` | ❌ `{"status":-1,"message":"股票代码不合法"}` | 北交所旧代码段 `43xxxx`/`83xxxx` **一律被拒** |
| `600519`（无前缀） | ❌ `股票代码不合法` | 必须带前缀 |
| `SH600519.SH`（带后缀） | ❌ `股票代码不合法` | 不接受 `.SH` 后缀 |
| `SZ600519`（前缀与代码不匹配） | ❌ `股票代码不合法` | 前缀必须与代码段一致 |
| ` SH600519`（前导空格） | ❌ `股票代码不合法` | 不 trim，调用方须自行 `strip()` |
| `999999` / `SH999999` / `SH605999`（从未发行） | ❌ `股票代码不合法` | |

**实现要点（供 PR-B）**：
1. 既有 `eastmoney._normalize_code` 是"去前缀去后缀 → 6 位"，**方向与本接口相反**：本接口需要**构造** `交易所前缀 + 6 位`。前缀规则实测可用判据：`6xxxxx/68xxxx → SH`、`0xxxxx/3xxxxx → SZ`、`920xxx → BJ`；`43xxxx/83xxxx` 本接口不可用（北交所老代码需先换成 920 新码，或该股无 F10 数据）。
2. 传入 `600519.SH` / `sz300750` 这类既有 canonical 形态时，必须**转换后**再调用，不能直接透传。

---

## 3. 出参总结构

顶层是**一个 JSON 对象**，固定 5 个键，全部为 `list`（无 `status`/`data` 包装）：

| 区块 | 名称（页面 Tab 对应） | 行数特征 | 内容 |
|---|---|---|---|
| `pjtj` | 评级统计 | 固定 5 行（时间窗 1/2/3/6 月内、1 年内） | 评级分布、5 分制均分 |
| `jgyc` | 机构预测 | `0` 或 `1 + N`（N≤30） | 第 0 行为**"近六月平均"聚合行**，其后为各机构预测 |
| `yctj_chart` | 预测统计（图表用） | 0~4 行 | 最近 1 个实际年度 + 3 个预测年度 |
| `yctj_list` | 预测统计（表格用） | 0~6 行（3 实际 + 3 预测） | **一致预期均值 + 各指标机构数** |
| `ycmx` | 预测明细 | 0~30 行 | 逐机构 EPS / 净利润 / 评级 |

页面 Tab 与区块对应关系经 F10 页面 HTML 实测（4 个 Tab 文案：`机构预测`/`评级统计`/`预测统计`/`预测明细`），**无"目标价"Tab**。

---

## 4. 五个区块的字段清单（实测键名，逐字照抄）

### 4.1 `pjtj[]` — 评级统计（17 键）

```text
SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, SECURITY_INNER_CODE, ORG_CODE,
SECURITY_TYPE_CODE, TRADE_MARKET_CODE, DATE_TYPE_CODE, DATE_TYPE,
COMPRE_RATING_NUM, COMPRE_RATING,
RATING_ORG_NUM, RATING_BUY_NUM, RATING_ADD_NUM, RATING_NEUTRAL_NUM,
RATING_REDUCE_NUM, RATING_SALE_NUM
```

- `DATE_TYPE_CODE`：`1`=1月内、`2`=2月内、`3`=3月内、`4`=6月内、`5`=1年内（`DATE_TYPE` 为同名中文）。
- `COMPRE_RATING_NUM`：**5 分制**综合评级均分（如 `4.816`）；`COMPRE_RATING`：中文标签（实测枚举 `买入`/`增持`/`中性`）。
- `RATING_ORG_NUM`：窗口内有评级的机构家数；`RATING_BUY_NUM`/`RATING_ADD_NUM`/`RATING_NEUTRAL_NUM`/`RATING_REDUCE_NUM`/`RATING_SALE_NUM`：分档家数。
- **空值形态**：窗口内无评级时该行除标识列外**全为 `null`**（不是 0）。

### 4.2 `jgyc[]` — 机构预测（22 键，含聚合行）

```text
SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, PUBLISH_DATE, ORG_CODE, ORG_NAME_ABBR,
YEAR1, YEAR_MARK1, EPS1, PE1,
YEAR2, YEAR_MARK2, EPS2, PE2,
YEAR3, YEAR_MARK3, EPS3, PE3,
YEAR4, YEAR_MARK4, EPS4, PE4
```

- **第 0 行是聚合行**：`ORG_CODE="00000000"`、`ORG_NAME_ABBR="近六月平均"`，`PUBLISH_DATE` = **数据快照日（当天）**，`EPS2/EPS3/EPS4` = 一致预期均值（与 `yctj_list` 的 `EPS` 逐位相同）。
- 其后为各机构行，按 `PUBLISH_DATE` 降序；**最多 30 条**（见 §7）。
- `YEAR_MARKn`：`A`=实际、`E`=预测。

### 4.3 `yctj_list[]` — 预测统计（20 键，**核心区块**）

```text
SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, YEAR, YEAR_MARK,
EPS, EPS_LASTMONTHS, BVPS, ROE, PARENT_NETPROFIT, TOTAL_OPERATE_INCOME, OPERATE_PROFIT,
EPS_COUNT, EPS_LASTMONTHS_COUNT, BVPS_COUNT, ROE_COUNT,
PARENT_NETPROFIT_COUNT, TOTAL_OPERATE_INCOME_COUNT, OPERATE_PROFIT_COUNT,
RANK
```

- `EPS` / `PARENT_NETPROFIT` / `TOTAL_OPERATE_INCOME` / `OPERATE_PROFIT` / `BVPS` / `ROE` 为**一致预期均值**。
- `*_COUNT` 为**参与该指标预测的机构家数**（`EPS_COUNT` 即设计所需 `coverage_count`）。实测各指标家数**互不相等**（如贵州茅台 FY1：`EPS_COUNT=44`、`ROE_COUNT=43`、`BVPS_COUNT=33`、`OPERATE_PROFIT_COUNT=41`）→ **必须显式选定 `EPS_COUNT`**，不可混用。
- `EPS_LASTMONTHS` + `EPS_LASTMONTHS_COUNT`：**上一期的同口径均值与家数**（实测存在家数变化：贵州茅台 `44 vs 46`、宁德时代 `33 vs 36`）。接口未给该列中文名，"上月快照"为据数值语义推断（见 §10）。
- `RANK`：行序号 1..N（图表序号，无业务含义）。
- 实际年度（`YEAR_MARK="A"`）行的全部 `*_COUNT` 为 `null`。

### 4.4 `yctj_chart[]` — 预测统计图表版（15 键）

```text
SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, YEAR, YEAR_MARK,
EPS, EPS_RATIO, PE, ROE,
PARENT_NETPROFIT, PARENT_NETPROFIT_RATIO,
TOTAL_OPERATE_INCOME, TOTAL_OPERATE_INCOME_RATIO, OPERATE_PROFIT, RANK
```

比 `yctj_list` 多出同比增速（`*_RATIO`）与 `PE`，**但没有 `*_COUNT`**。→ 机构数只能取 `yctj_list`。

### 4.5 `ycmx[]` — 预测明细（25 键）

```text
SECUCODE, SECURITY_CODE, SECURITY_NAME_ABBR, SECURITY_INNER_CODE,
PUBLISH_DATE, ORG_CODE, ORG_NAME_ABBR, RESEARCHER,
YEAR1, YEAR_MARK1, EPS1, PARENT_NETPROFIT1,
YEAR2, YEAR_MARK2, EPS2, PARENT_NETPROFIT2,
YEAR3, YEAR_MARK3, EPS3, PARENT_NETPROFIT3,
YEAR4, YEAR_MARK4, EPS4, PARENT_NETPROFIT4,
RATING
```

- 每行 = 一家机构一次预测，含 `PARENT_NETPROFITn`（归母净利，元）与 `RATING`（实测枚举：`买入` / `增持` / `持有` / `中性`，可为 `null`）。
- **无上期评级字段**（只有当前 `RATING`）→ 无升级/降级计数。
- **最多 30 条**（见 §7）。

---

## 5. 三个必需样本的原始响应摘录

> 全部为**原始 JSON 逐字摘录**（未改键名、未改精度）。完整响应见复现命令 §11。

### 5.1 沪主板大市值：`SH600519` 贵州茅台（响应 37,686 B）

`pjtj`（`DATE_TYPE_CODE=5`，1 年内）：
```json
{"SECUCODE": "600519.SH", "SECURITY_NAME_ABBR": "贵州茅台", "SECURITY_INNER_CODE": "1000002162", "ORG_CODE": "10002602", "SECURITY_TYPE_CODE": "058001001", "TRADE_MARKET_CODE": "069001001001", "DATE_TYPE_CODE": 5, "DATE_TYPE": "1年内", "COMPRE_RATING_NUM": 4.816326530612, "COMPRE_RATING": "买入", "RATING_ORG_NUM": 49, "RATING_BUY_NUM": 40, "RATING_ADD_NUM": 9, "RATING_NEUTRAL_NUM": null, "RATING_REDUCE_NUM": null, "RATING_SALE_NUM": null, "SECURITY_CODE": "600519"}
```
`jgyc[0]`（聚合行，注意 `PUBLISH_DATE` = 当天）：
```json
{"SECUCODE": "600519.SH", "SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台", "PUBLISH_DATE": "2026-10-19 00:00:00", "ORG_CODE": "00000000", "ORG_NAME_ABBR": "近六月平均", "YEAR1": 2025, "YEAR_MARK1": "A", "EPS1": 65.851754826108, "PE1": 20.12049646, "YEAR2": 2026, "YEAR_MARK2": "E", "EPS2": 67.249318181818, "PE2": 19.694842176449, "YEAR3": 2027, "YEAR_MARK3": "E", "EPS3": 71.125909090909, "PE3": 18.621857437932, "YEAR4": 2028, "YEAR_MARK4": "E", "EPS4": 75.18, "PE4": 17.617715927678}
```
`yctj_list`（预测年度行，`YEAR_MARK="E"`）：
```json
{"SECUCODE": "600519.SH", "SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台", "YEAR": 2026, "YEAR_MARK": "E", "EPS": 67.249318181818, "EPS_LASTMONTHS": 67.623260869565, "BVPS": 218.344848484849, "ROE": 31.190465116279, "PARENT_NETPROFIT": 84099207500, "TOTAL_OPERATE_INCOME": 176968699545.455, "OPERATE_PROFIT": 116957750975.61, "EPS_COUNT": 44, "EPS_LASTMONTHS_COUNT": 46, "BVPS_COUNT": 33, "ROE_COUNT": 43, "PARENT_NETPROFIT_COUNT": 44, "TOTAL_OPERATE_INCOME_COUNT": 44, "OPERATE_PROFIT_COUNT": 41, "RANK": 4}
{"...YEAR": 2027, "YEAR_MARK": "E", "EPS": 71.125909090909, "EPS_COUNT": 44, ...}
{"...YEAR": 2028, "YEAR_MARK": "E", "EPS": 75.18, "EPS_COUNT": 43, ...}
```
`ycmx[0]`（机构明细）：
```json
{"SECUCODE": "600519.SH", "SECURITY_CODE": "600519", "SECURITY_NAME_ABBR": "贵州茅台", "SECURITY_INNER_CODE": "1000002162", "PUBLISH_DATE": "2026-09-21 00:00:00", "ORG_CODE": "10030527", "ORG_NAME_ABBR": "诚通证券", "RESEARCHER": "陈文倩", "YEAR1": 2025, "YEAR_MARK1": "A", "EPS1": 65.851754826108, "PARENT_NETPROFIT1": 82320067101.68, "YEAR2": 2026, "YEAR_MARK2": "E", "EPS2": 65.14, "PARENT_NETPROFIT2": 81424000000, "YEAR3": 2027, "YEAR_MARK3": "E", "EPS3": 67.87, "PARENT_NETPROFIT3": 84844000000, "YEAR4": 2028, "YEAR_MARK4": "E", "EPS4": 71.32, "PARENT_NETPROFIT4": 89160000000, "RATING": "买入"}
```
容量：`jgyc=31`（1 聚合 + 30 机构）、`ycmx=30`、`EPS_COUNT=44` → **明细少于机构数**。

### 5.2 深主板中市值：`SZ000333` 美的集团（响应 26,371 B）

`pjtj`（1 年内）：
```json
{"DATE_TYPE_CODE": 5, "DATE_TYPE": "1年内", "COMPRE_RATING_NUM": 4.764705882353, "COMPRE_RATING": "买入", "RATING_ORG_NUM": 34, "RATING_BUY_NUM": 26, "RATING_ADD_NUM": 8, "RATING_NEUTRAL_NUM": null, "RATING_REDUCE_NUM": null, "RATING_SALE_NUM": null}
```
`jgyc[0]`：
```json
{"PUBLISH_DATE": "2026-10-19 00:00:00", "ORG_CODE": "00000000", "ORG_NAME_ABBR": "近六月平均", "YEAR2": 2026, "YEAR_MARK2": "E", "EPS2": 6.280034482759, "YEAR3": 2027, "YEAR_MARK3": "E", "EPS3": 6.62, "YEAR4": 2028, "YEAR_MARK4": "E", "EPS4": 7.147586206897}
```
`yctj_list`（2026E / 2027E）：
```json
{"YEAR": 2026, "YEAR_MARK": "E", "EPS": 6.280034482759, "EPS_LASTMONTHS": 6.195193548387, "PARENT_NETPROFIT": 47811579655.1724, "EPS_COUNT": 29, "EPS_LASTMONTHS_COUNT": 31, "ROE_COUNT": 29, "PARENT_NETPROFIT_COUNT": 29}
{"YEAR": 2027, "YEAR_MARK": "E", "EPS": 6.62, "EPS_LASTMONTHS": 6.629677419355, "EPS_COUNT": 29}
```
容量：`jgyc=20`（1+19）、`ycmx=19`、`EPS_COUNT=29`（与 5.1 同为"明细<机构数"）。

### 5.3 创业板小市值：`SZ301489` 思泉新材（响应 7,872 B，自选样本）

`pjtj`（1 年内）：
```json
{"DATE_TYPE_CODE": 5, "DATE_TYPE": "1年内", "COMPRE_RATING_NUM": 4.75, "COMPRE_RATING": "买入", "RATING_ORG_NUM": 4, "RATING_BUY_NUM": 3, "RATING_ADD_NUM": 1, "RATING_NEUTRAL_NUM": null, "RATING_REDUCE_NUM": null, "RATING_SALE_NUM": null}
```
`jgyc[0]`：
```json
{"PUBLISH_DATE": "2026-10-19 00:00:00", "ORG_CODE": "00000000", "ORG_NAME_ABBR": "近六月平均", "YEAR2": 2026, "YEAR_MARK2": "E", "EPS2": 2.745, "YEAR3": 2027, "YEAR_MARK3": "E", "EPS3": 5.815, "YEAR4": 2028, "YEAR_MARK4": "E", "EPS4": 8.365}
```
`yctj_list`（2026E）：
```json
{"YEAR": 2026, "YEAR_MARK": "E", "EPS": 2.745, "EPS_LASTMONTHS": 3.49, "PARENT_NETPROFIT": 257500000, "TOTAL_OPERATE_INCOME": 1786500000, "EPS_COUNT": 2, "EPS_LASTMONTHS_COUNT": 1, "BVPS_COUNT": 1, "ROE_COUNT": 2, "PARENT_NETPROFIT_COUNT": 2}
```
`ycmx` 仅 1 行（东北证券，`RATING="增持"`，`EPS2=3.49`）→ **`EPS_COUNT=2` 但明细只有 1 条**，两家的均值 `2.745` **不等于**明细唯一值的 `3.49`：聚合均值包含了明细列表未暴露的机构（详见 §7/§10）。

> **小盘警示**：该样本的 `EPS_LASTMONTHS=3.49` 与 `EPS=2.745` 相差 27%，且 `EPS_LASTMONTHS_COUNT=1` 而 `EPS_COUNT=2` —— 样本更换家数会剧烈扰动"1 个月修正"口径，PR-B 若用该列算 revision 必须在家数变动时降级（写入 warnings）。

### 5.4 补充样本（跨板块确认字段稳定）

| 样本 | 板块 | `jgyc` | `ycmx` | `EPS_COUNT`(FY1) | `COMPRE_RATING_NUM`(1年内) |
|---|---|---|---|---|---|
| `SZ300750` 宁德时代 | 创业板大市值 | 24（1+23） | 23 | 33 | 4.837 |
| `SH688981` 中芯国际 | 科创板 | 10（1+9） | 10 | 11 | 4.824 |

五个字段区块的键集合在沪主板/深主板/创业板/科创板样本间**完全一致**（无一字段缺失或多出）。

---

## 6. 空值形态（实测三类 + 一类错误）

### 6.1 无机构覆盖（`pjtj=[]`、`jgyc=[]`、`ycmx=[]`，只有历史实际值）

实测样本：`SH600112` *ST天成、`SZ000995` 皇台酒业、`SH603207` 小方制药。

```json
{"pjtj": [], "jgyc": [], "yctj_list": [
  {"YEAR": 2023, "YEAR_MARK": "A", "EPS": -0.418323368725, ..., "EPS_COUNT": null},
  {"YEAR": 2024, "YEAR_MARK": "A", "EPS": -0.28390506427,  ..., "EPS_COUNT": null},
  {"YEAR": 2025, "YEAR_MARK": "A", "EPS": -0.23828398218,  ..., "EPS_COUNT": null}],
 "yctj_chart": [ /* 仅最新实际年度 1 行 */ ], "ycmx": []}
```

**判据（供 PR-B 落地）**：
- 覆盖率 0 的可靠信号 = **`jgyc` 为空 且 `yctj_list` 中不存在 `YEAR_MARK=="E"` 的行**；
- 绝不能只看 `EPS is None`（无覆盖时 `yctj_list` 根本不给预测年度行，不是"给了行但值为 null"）；
- 注意 `yctj_list` **仍非空**（历史实际年度照常返回），因此"`yctj_list` 非空 ⇒ 有覆盖"是**错误判据**。

### 6.2 次新股（有评级但无一致预期）

实测样本：`SH603202` 天有为（2025 年上市）。

- `pjtj`：**5 行齐全但前 4 行全 `null`**，仅"1年内"行有值（`COMPRE_RATING_NUM=5`、`RATING_ORG_NUM=1`、`RATING_BUY_NUM=1`）：

```json
{"DATE_TYPE_CODE": 1, "DATE_TYPE": "1月内", "COMPRE_RATING_NUM": null, "COMPRE_RATING": null, "RATING_ORG_NUM": null, ...}
{"DATE_TYPE_CODE": 5, "DATE_TYPE": "1年内", "COMPRE_RATING_NUM": 5, "COMPRE_RATING": "买入", "RATING_ORG_NUM": 1, "RATING_BUY_NUM": 1, "RATING_ADD_NUM": null, ...}
```
- `jgyc=[]`、`ycmx=[]`、`yctj_list` 只有 3 个实际年度、**无任何 E 行** → 一致预期 EPS / coverage 全部缺失。
- **重要**：`pjtj` 有评级不等于有一致预期 —— 评级与预测是两个独立区块，PR-B 不能由 `pjtj` 有值推断 `eps_fy1` 有值。

### 6.3 退市/长期停牌类（结构与"无覆盖"同形，实际年度陈旧）

实测样本：`SH600608`（退市沪科）、`SH600091`（退市明科）、`SZ000613`（东海A退）、`SH600145`（退市新亿）、`SZ000033`（新都退）、`SH600087`（退市长油）。

```json
{"pjtj": [], "jgyc": [], "yctj_chart": [{"SECURITY_NAME_ABBR": "退市新亿", "YEAR": 2020, "YEAR_MARK": "A", "EPS": -0.086689163911, ...}], "yctj_list": [...], "ycmx": []}
```
形态与 §6.1 完全一致，唯一差别是**最新实际年度陈旧**（退市新亿停在 2020）。→ 陈旧度需由调用方用 `YEAR` 与当前年份比对，接口本身不给"是否停牌"标志。

### 6.4 非法代码（唯一会返回 `status` 的形态）

```json
{"status": -1, "message": "股票代码不合法"}
```
HTTP 状态码仍是 **200**（不是 4xx/5xx），响应体仅 47 字节。→ 调用方**不能只靠 `response.status_code` 判成功**，必须解析 JSON 并检查是否含 `pjtj`/`jgyc` 等键（或检查 `status == -1`）。

---

## 7. 分页与容量（重要，直接影响 max/min 与 revision 口径）

1. **接口无分页**：所有分页类参数被忽略（§1.2），单次返回该股全部可用数据。
2. **列表级截断（实测上限 30）**：

| 样本 | `EPS_COUNT`(FY1) | `ycmx` 行数 | `jgyc` 行数 | 是否被截断 |
|---|---|---|---|---|
| SH600519 贵州茅台 | 44 | **30** | 31 | 是（上限 30） |
| SZ300750 宁德时代 | 33 | 23 | 24 | 否（近 6 月内仅 23 家） |
| SZ000333 美的集团 | 29 | 19 | 20 | 否 |
| SZ002594 比亚迪 | 35 | 25 | 26 | 否 |
| SH603257 中国瑞林 | 2 | **1** | 2 | 是（另一家早于 6 个月窗口） |
| SZ301489 思泉新材 | 2 | 1 | 2 | 是 |

   结论：`ycmx`/`jgyc` 是"**近 6 个月 + 最多 30 条**"的明细窗口，**不是**全部覆盖机构；而 `yctj_list.EPS_COUNT` 是**全量家数**。

3. **对 PR-B 的直接后果**：
   - **EPS 最高/最低无法准确取得**：只能用 `ycmx` 明细自算，样本被截断时（如贵州茅台 44 家只给 30 家）算出的 min/max **区间偏窄**，会低估分歧度。若 PR-B 需要 max/min，必须带"是否截断"标记（`len(ycmx) < EPS_COUNT`）或干脆不落。
   - `EPS`/`PARENT_NETPROFIT` 均值**不受截断影响**（`yctj_list` 是全量聚合，实测 `yctj_list.EPS == jgyc[0].EPSn` 逐位相同）。
   - 均值口径验证：在明细完整的两只股票上，**`yctj_list.EPS` 精确等于 `ycmx` 各机构 EPS 的算术均值**：
     - `SZ301631` 壹连科技 2026E：`EPS_COUNT=3`、明细 3 条均值 `2.8666666666666667` = `yctj_list.EPS 2.866666666667` ✅
     - `SZ301628` 强达电路 2026E：`EPS_COUNT=2`、明细 2 条均值 `2.255` = `yctj_list.EPS 2.255` ✅
   - 反向证据（截断时不等）：`SH603257` 中国瑞林 2026E：`EPS_COUNT=2`，明细仅 1 条 `EPS2=1.81`，而 `yctj_list.EPS=1.92` ≠ 1.81 → 均值分母是全量家数。

---

## 8. 东财 F10 字段 → canonical 映射表（对照设计 §5.1，**不符处以本表实测为准**）

### 8.1 可映射（实测有字段、语义清楚）

| canonical | 东财字段（实测） | 取数规则 | 单位/类型 | 备注 |
|---|---|---|---|---|
| `eps_fy1` | `yctj_list[].EPS` | 取 `YEAR_MARK=="E"` 的**第 1 行** | CNY_PER_SHARE / float | 2026-10-19 实测该行为 `YEAR=2026`（**当前财年**） |
| `eps_fy2` | `yctj_list[].EPS` | `YEAR_MARK=="E"` 的**第 2 行** | 同上 | 实测 `YEAR=2027` |
| `eps_fy3` | `yctj_list[].EPS` | `YEAR_MARK=="E"` 的**第 3 行**（可缺） | 同上 | 实测存在（`YEAR=2028`），覆盖率低于 FY1/FY2（贵州茅台 43 vs 44） |
| `coverage_count` | `yctj_list[].EPS_COUNT` | 取与 `eps_fy1` **同一行** | int / 家数 | 与 `PARENT_NETPROFIT_COUNT`/`ROE_COUNT` 可能不等，**固定用 `EPS_COUNT`** |
| `as_of_date` | `jgyc[0].PUBLISH_DATE` | 取日期部分（`YYYY-MM-DD`） | date | 实测该行恒为 `ORG_CODE="00000000"`、`ORG_NAME_ABBR="近六月平均"`、`PUBLISH_DATE`=**当天**（贵州茅台最新机构研报为 2026-09-21 时该行仍为 2026-10-19）→ 它是**数据刷新日**而非研报日；`jgyc` 为空时退化为请求日 |
| （可选）FY1/FY2 归母净利 | `yctj_list[].PARENT_NETPROFIT` | 与 EPS 同行 | CNY / float | 设计 §5.1 标注"可选"，实测有值（如贵州茅台 2026E `84,099,207,500`） |
| （可选）FY1 营收/ROE | `yctj_list[].TOTAL_OPERATE_INCOME` / `ROE` | 同上 | CNY / percent | 本设计不需要 |

### 8.2 设计 §5.1 期望但**实测不存在 / 口径不符**（必须以实测收敛）

| 设计 §5.1 期望 | 实测结论 | PR-B 处置建议 |
|---|---|---|
| "预测年度（序列）"：`fy1/fy2 取自当前年+1/+2` | 实测 `YEAR_MARK=="E"` 首行是**当前财年**（2026），次行 2027、再次 2028；`jgyc[0].YEAR2=2026` 亦印证。该表述若读作"从当前年+1 起算"会把 FY1 错取成 2027；若读作"当前年、+1、+2"则与实测一致（**表述歧义，须以实测收敛**） | **按实测改口径**：FY1/FY2/FY3 = 第 1/2/3 个 `YEAR_MARK=="E"` 行（= 当前财年/次年/次次年，与既有 `predictThisYearEps`→`eps_fy1` 口径一致） |
| "EPS 均值/最高/最低"（均值→`eps_fy1/fy2`） | **均值有**（`yctj_list.EPS`）；**最高/最低接口不提供**：全响应键名扫描无 `MAX`/`MIN`/`HIGH`/`LOW` 类字段；只能由 `ycmx[].EPSn` 自算，且明细被截断到 30 条（§7） | 均值按 §8.1 映射；**max/min 不落 canonical**（无对应字段），如需分歧度只在快照里带 `eps_min/eps_max/eps_detail_count/eps_count` 并在 `len(ycmx)<EPS_COUNT` 时标 `truncated=true` |
| "评级分布 → `rating_mean`/升降级计数" | 分布**有**（`pjtj`：`RATING_ORG_NUM`/`RATING_BUY_NUM`/…，5 个时间窗）；但 **`COMPRE_RATING_NUM` 是 5 分制**（实测 4.75~4.84），而既有 report_list 路径的 `rating_mean` 是东财 `emRatingValue` 的 **3 分制**（持有1/增持2/买入3，见 `schemas/consensus.yaml`）。**两套量表不可混比**，且 `pjtj` **无上期评级** → **无升级/降级计数** | `rating_mean` **不要**直接用 F10 值（会污染与 report_list 帧的可比性）；`rating_upgrade_1m`/`rating_downgrade_1m`/`revision_breadth` **本接口不可得** → 置 `None` + `consensus_missing` |
| "目标价（若接口含）" | **完全不存在**：25 个区块键名逐一扫描 + 对原始响应 grep `target`/`目标价` 均为 0 命中；页面只有 4 个 Tab（机构预测/评级统计/预测统计/预测明细），无目标价 Tab | `target_price` **恒为 `None`**，显式产出 `consensus_missing: target_price`（与既有 report_list 的稀疏目标价相比更差：这里是 0 覆盖）；若需保留目标价只能继续走 report_list |
| （设计未提）上月快照 | 实测存在 `EPS_LASTMONTHS` + `EPS_LASTMONTHS_COUNT`（上一期同口径均值与家数，家数可与当期不同） | 可直接作为 `eps_fy1_revision_1m` 的分子/分母来源，**无需等自建快照积累**；但家数变动大时（如思泉新材 1→2、贵州茅台 46→44）须在 `warnings` 留痕 |

### 8.3 13 个 canonical 字段的完整可得性矩阵（`schemas/consensus.yaml` 口径）

| canonical | 本接口可得性 | 来源 |
|---|---|---|
| `eps_fy1` | ✅ 直接 | `yctj_list[E1].EPS` |
| `eps_fy2` | ✅ 直接 | `yctj_list[E2].EPS` |
| `eps_fy3` | ✅ 直接（可缺） | `yctj_list[E3].EPS` |
| `coverage_count` | ✅ 直接 | `yctj_list[E1].EPS_COUNT` |
| `target_price` | ❌ 不存在 | 恒 `None` + `consensus_missing` |
| `rating_mean` | ⚠️ 有值但**量表不同**（5 分制 vs 3 分制） | `pjtj[DATE_TYPE_CODE=4/5].COMPRE_RATING_NUM`（不建议映射） |
| `eps_fy1_revision_1m` | ✅ 可算（同响应内） | `EPS/EPS_LASTMONTHS - 1`（口径待 §10 确认） |
| `eps_fy2_revision_1m` | ✅ 可算（同响应内） | 同上（FY2 行） |
| `eps_fy1_revision_3m` | ❌ 本接口无 3 个月前值 | 需自建快照序列（设计 §5.2） |
| `eps_fy2_revision_3m` | ❌ | 同上（该字段 canonical 未定义，按现状不落） |
| `revision_breadth` | ❌ 无上期评级 | `None` |
| `revision_acceleration` | ❌ 需快照序列二阶差分 | 自建快照 |
| `rating_upgrade_1m` | ❌ 无上期评级 | `None` |
| `rating_downgrade_1m` | ❌ 同上 | `None` |

---

## 9. 与设计 §5.3 相关的额外实测约束（供 PR-B 设计复核）

1. **快照日的可靠来源**：`jgyc[0].PUBLISH_DATE`（聚合行，恒为当天）——比"本地写入日"更接近东财口径；但注意它是**服务端刷新的数据日期**（0 点整），与"我们读到它的时刻"可能差 1 天（跨日）。设计 §5.4 说"新源快照的 as_of = 快照写入日"，建议改成"聚合行 `PUBLISH_DATE` 日期，缺失时回落本地写入日"。
2. **同日幂等**：由于 CDN 缓存 120 s（§1.4），同一天内多次抓取可能得到同一份数据；`append_snapshot` 的同日幂等策略与之一致，无需特殊处理。
3. **code 归一化方向相反**：既有 `collectors/consensus/eastmoney._normalize_code` 是"去前缀"；本接口要"加前缀"。新模块若复用该函数，必须**反向补前缀**（§2），否则全部返回"股票代码不合法"。
4. **失败判定**：接口对"代码不合法"仍返回 HTTP 200 + `{"status":-1}`，`raise_for_status()` 不会触发。fail-open 逻辑必须**解析 JSON 后判键**（缺 `pjtj`/`jgyc` 即视为失败）。
5. **北交所覆盖**：`92xxxx` 可用、`43xxxx`/`83xxxx` 不可用 —— 若 PR-B 对有北交所老代码的标的调用，会稳定拿到"股票代码不合法"，应直接跳过（回落 report_list）。
6. **`yctj_list` 非空 ≠ 有覆盖**（§6.1）：无覆盖时仍有 3 行实际年度。若 PR-B 用"`yctj_list` 是否为空"做存在性判断，会把"无覆盖"误判为"有覆盖、coverage=None"。

---

## 10. 未验证项与局限（显式声明，不编造）

| 项 | 状态 | 说明 |
|---|---|---|
| 停牌个股的独立样本 | **未取得** | 尝试通过 `push2.eastmoney.com/api/qt/clist/get` 全市场扫描 `f2` 为空的停牌标的，该行情接口在连续分页请求下被限流（返回非 JSON 空体），未能定位真实停牌样本。已用**退市/退市整理期样本**（§6.3）作为近邻实测：形态与"无覆盖"同形，无额外字段变化。**F10 是财报+研报快照，理论上与交易状态无关**（未验证，勿当结论引用） |
| `EPS_LASTMONTHS` 的精确语义 | **推断** | 接口未返回该列中文名（页面 JS 与静态资源未取到列定义，`ProfitForecast.js` 路径 404）。实测表现为"上一期同口径均值 + 上一期家数"（家数可与当期不同）。是否为"上月"还是"上次刷新"，建议 PR-B 用两日快照对拍确认后再用于 `revision_1m` |
| 网络失败 / 限流形态 | **未发生** | 本次冒烟 20+ 次请求全部 200，未出现超时、429、5xx；fail-open 行为只能由实现侧 try/except 保证，属设计约束而非实测结论 |
| EPS max/min 的官方口径 | **不存在** | 不是"没测到"，是**接口确实不提供**（键名全量扫描 + 原文 grep 双证） |
| 港股/美股适配 | 不适用 | 本接口 `SECUCODE` 带 `.SH/.SZ/.BJ`，仅 A 股；`TRADE_MARKET_CODE` 实测值 `069001001001`(沪主板)/`069001002001`(深主板)/`069001002002`(创业板)/`069001001006`(科创板)/`069001017`(北交所) |

---

## 11. 复现命令（原始响应可随时重跑核对）

```bash
# 主样本（3 个）
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519' | python3 -m json.tool | head -60
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SZ000333' | python3 -m json.tool | head -60
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SZ301489' | python3 -m json.tool | head -60

# 空值形态
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600112'   # 无覆盖（*ST）
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH603202'   # 次新（有评级无预测）
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600608'   # 退市

# 入参格式反例
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=600519'     # → 股票代码不合法
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SZ600519'   # → 股票代码不合法

# 分页参数无效性（三个 md5 应完全相同）
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519' | md5sum
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519&pageNum=2' | md5sum
curl -s 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519&ps=200' | md5sum

# 请求头无要求（三份字节数应均为 37686）
curl -s -o /dev/null -w '%{http_code} %{size_download}\n' 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519'
curl -s -H 'User-Agent:' -o /dev/null -w '%{http_code} %{size_download}\n' 'https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=SH600519'
```

---

## 12. 冒烟结论（给 PR-B 的三条硬约束）

1. **EPS 均值 + 机构数可以放心用**：`yctj_list[].EPS` 是全量家数均值（已在两只明细完整的样本上精确验证）、`EPS_COUNT` 是全量机构数，二者同行取值即可。
2. **`target_price` 与 max/min 必须显式降级**：目标价 0 覆盖（`consensus_missing`）；max/min 仅能从被截断的明细自算，需带 `truncated` 标记，否则会系统性低估分歧。
3. **年度锚定按 `YEAR_MARK=="E"` 行序取 FY1/FY2/FY3**（当前财年/次年/次次年 = 2026/2027/2028）；设计 §5.1 的"当前年+1/+2"表述歧义，按字面"从当前年+1 起算"会错取成 2027/2028；评级相关 canonical（`rating_mean` 及升降级计数）在新源上口径不可比，保持 `None` 或不映射。
