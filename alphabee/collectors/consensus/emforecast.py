"""东方财富 F10「盈利预测」数据源（E 生态设计 §5.2；契约以冒烟报告实测为准）。

本模块是 *source-specific collector*：只在这里出现东财 F10 原始键名，输出一律 canonical
（``alphabee/schemas/consensus.yaml``）。它提供三件事：

1. :func:`fetch_profit_forecast` —— 单次快照拉取（fail-open 返回 ``{}``，绝不抛）；
2. :func:`append_snapshot` / :func:`load_snapshots` —— 快照 append-only 落盘
   ``<data_root>/consensus_snapshots/<6位代码>.jsonl``（**文件名单一形态**：``300750.SZ`` /
   ``300750`` / ``sz300750`` 都落同一个 ``300750.jsonl``；损坏行跳过、同日幂等；
   写盘/读盘 ``OSError`` 一律 fail-open 不抛）；
3. :func:`compute_revisions` / :func:`build_f10_consensus` —— 由快照序列**纯规则**自算
   1m/3m revision（复用 :mod:`engine` 既有窗口原语，无 LLM、可离线重放）。

**fail-open 纪律**（本模块对外承诺「绝不抛」，三条硬化点）：

- **区块类型守卫**：``pjtj`` / ``jgyc`` / ``yctj_list`` 任一不是 ``list``（如接口异常时返回
  ``5`` / ``null``）⇒ 整份响应按不可用处理（``{}`` + 原因），绝不进入迭代抛 ``TypeError``；
  ``yctj_chart`` / ``ycmx`` 本模块不消费，非 list 只记 warning、不判死（不让旁支区块毁掉可用数据）。
- **写盘失败 fail-open**：``mkdir`` / ``open`` / ``write`` 的 ``OSError``（只读目录、磁盘满、
  权限）全部捕掉 → 不抛、不回落；**当次快照（内存值）仍参与当次输出与 revision 计算**，
  并在 ``ConsensusOutput.warnings`` 留痕「落盘失败」。
- **revision 与落盘解耦**：revision 序列恒为「磁盘历史 + 当次内存快照」（同 ``as_of_date``
  以历史行优先，与 ``append_snapshot`` 的同日幂等一致），因此落盘成功/失败算出的 revision
  逐位相同，当期点不会因未落盘而丢失。

接口冒烟结论（2026-10-19 实测，报告：``docs/implementation/midterm/E_ECOSYSTEM_SMOKE_REPORT.md``）
────────────────────────────────────────────────────────────────────────────────
**地址**：``GET https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax?code=<前缀+6位>``
（备用域名 ``emweb.eastmoney.com`` 字节级一致）。

**入参**：仅 ``code`` 一个业务参数，必须「交易所前缀 + 6 位」：``SH600519`` / ``SZ000333`` /
``BJ920002``；前缀大小写不敏感。无前缀、``.SH`` 后缀、前缀与代码段错配、前导空格一律返回
``{"status": -1, "message": "股票代码不合法"}``（**HTTP 仍是 200**）。北交所仅 ``920xxx`` 可用，
``43xxxx`` / ``83xxxx`` 被拒 → 本模块不发请求、直接 fail-open。无分页参数（pageNum/page/ps/pageSize
被忽略，md5 相同）。

**请求头**：无需 UA / Referer / Cookie / Origin（显式空 UA 仍 200）；CDN 缓存 120 秒。

**出参**：单个 JSON 对象，5 个区块 ``pjtj``（评级统计） / ``jgyc``（机构预测） /
``yctj_chart`` / ``yctj_list``（预测统计，**核心**） / ``ycmx``（预测明细）。

**实测字段 → canonical**（冒烟报告 §8.1；与设计 §5.1 的接口名假设不符处一律以实测为准）：

============================  ==========================================  ===================================
canonical                     东财字段（实测）                            取数规则
============================  ==========================================  ===================================
``eps_fy1``                   ``yctj_list[].EPS``                         ``YEAR_MARK == "E"`` 的**第 1 行**
``eps_fy2``                   ``yctj_list[].EPS``                         同上第 2 行
``eps_fy3``                   ``yctj_list[].EPS``                         同上第 3 行（可缺）
``coverage_count``            ``yctj_list[].EPS_COUNT``                   与 ``eps_fy1`` **同一行**的家数
``as_of_date``                ``jgyc[0].PUBLISH_DATE``                    聚合行（``ORG_CODE=00000000``）；
                                                                          缺 ``jgyc`` → 本地抓取日
（快照审计用）                ``yctj_list[].EPS_LASTMONTHS`` /            上一期均值与家数；**不参与** revision
                              ``EPS_LASTMONTHS_COUNT``                    计算（见下），只随快照留痕
============================  ==========================================  ===================================

**实测修正的关键口径**：

- **年度锚定**：``YEAR_MARK == "E"`` 首行是**当前财年**（实测 2026），次行 2027、再次 2028；
  设计 §5.1「当前年+1/+2」若读作"从当前年+1 起算"会错取 2027/2028 —— 本模块按 E 行行序取
  FY1/FY2/FY3（行序即年度升序，与既有 ``predictThisYearEps`` → ``eps_fy1`` 口径一致）。
- **``target_price`` 完全不存在**（全响应无任何 target/目标价键，页面亦无目标价 Tab）→ 恒
  ``None`` + ``consensus_missing: target_price``，绝不回退 0。
- **EPS 最高/最低接口不提供**；只能从 ``ycmx`` 自算，而 ``ycmx``/``jgyc`` 是"近 6 个月 + 最多
  30 条"的窗口（贵州茅台 ``EPS_COUNT=44`` 但明细 30 条）→ 自算必偏窄，故本模块**不落 max/min**。
- **评级量表不可比**：``pjtj[].COMPRE_RATING_NUM`` 是 **5 分制**（实测 4.75~4.84），而既有
  ``report_list`` 路径的 ``rating_mean`` 是东财 ``emRatingValue`` 的 **3 分制**（持有1/增持2/买入3）；
  且 ``pjtj`` 无上期评级 → ``rating_mean`` / ``rating_upgrade_1m`` / ``rating_downgrade_1m`` /
  ``revision_breadth`` 在新源上保持 ``None`` + ``consensus_missing``（不污染与 report_list 帧的可比性）。

**空值形态**（冒烟报告 §6）：无覆盖 / 次新 / 退市一律 HTTP 200，``pjtj`` / ``jgyc`` / ``ycmx``
为空数组，且 ``yctj_list`` **仍返回 3 个实际年度但没有 ``YEAR_MARK == "E"`` 的行** —— 因此
判据是"``jgyc`` 为空且无 E 行"，**不是**"``yctj_list`` 是否为空"。次新股 ``pjtj`` 有评级但
``jgyc`` 为空，评级与一致预期是两个独立区块。本模块把上述形态统一视为"无可用快照" →
:func:`fetch_profit_forecast` 返回 ``{}``、:func:`build_f10_consensus` 返回 ``None``
（由 ``eastmoney.build_consensus`` 的 provider 链回落 report_list 并留痕）。

**revision 自算**（设计 §5.2）：本接口只给当前快照、无历史时序，故
``eps_fy1_revision_1m/3m``（与 ``eps_fy2_revision_1m``、``revision_acceleration``）由
``consensus_snapshots`` 序列按"当期值 vs 窗口前最近值"的变化率计算，窗口原语直接复用
:mod:`alphabee.collectors.consensus.engine`（``_latest_at_or_before`` / ``_pct_change`` /
``_revision_series``）——纯规则、无 LLM、可离线重放。快照不足窗口 → ``None``（不编造）。
"""

from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import requests

from alphabee.collectors.consensus.engine import (
    CONSENSUS_FIELDS,
    ConsensusOutput,
    _latest_at_or_before,
    _parse_date,
    _pct_change,
    _revision_series,
    _to_float,
)

logger = logging.getLogger(__name__)

#: F10 盈利预测页接口（冒烟实测：无鉴权、无分页、无请求头要求）
F10_FORECAST_URL = "https://emweb.securities.eastmoney.com/PC_HSF10/ProfitForecast/PageAjax"
#: ``ConsensusOutput.source`` 自证来源（设计 §5.4；报告/账本可审计）
SOURCE_NAME = "eastmoney:f10/profit_forecast"
#: 快照落盘目录名（相对 ``get_data_root()``，即默认 ``data/consensus_snapshots/``）
SNAPSHOT_DIR_NAME = "consensus_snapshots"
#: ``jgyc`` 第 0 行（"近六月平均"聚合行）的机构代码，其 ``PUBLISH_DATE`` 即数据刷新日
_AGGREGATE_ORG_CODE = "00000000"
#: 交易所前缀（实测可用判据，冒烟 §2）
_EXCHANGE_PREFIXES = ("SH", "SZ", "BJ")
#: 响应必须包含、且类型必须是 ``list`` 的区块（缺一/类型不符即视为非法响应，不能只看 HTTP 200）
_REQUIRED_BLOCKS = ("pjtj", "jgyc", "yctj_list")
#: 本模块不消费的旁支区块：仅做防御性类型检查（非 list 只记 warning，不判死可用数据）
_UNUSED_BLOCKS = ("yctj_chart", "ycmx")
#: revision 窗口（源字段, canonical 字段, 天数）——与 engine 的 30/90 天窗口同构
_REVISION_WINDOWS: tuple[tuple[str, str, int], ...] = (
    ("eps_fy1", "eps_fy1_revision_1m", 30),
    ("eps_fy1", "eps_fy1_revision_3m", 90),
    ("eps_fy2", "eps_fy2_revision_1m", 30),
)


# ─────────────────────────────────────────────────────────────────────────────
# code 入参归一（本接口要"补前缀"，与 eastmoney._normalize_code 的"去前缀"方向相反）
# ─────────────────────────────────────────────────────────────────────────────
def _infer_prefix(digits: str) -> str:
    """由 6 位代码段推断交易所前缀（实测判据）；无法可靠推断 → ``""``。"""
    if digits.startswith("92"):
        return "BJ"
    if digits.startswith("6"):
        return "SH"
    if digits.startswith(("0", "3")):
        return "SZ"
    return ""


def market_prefixed_code(code: str) -> str:
    """``600519`` / ``600519.SH`` / ``sh600519`` → ``SH600519``（本接口唯一合法入参形态）。

    无法构造合法入参（北交所旧代码段、非 6 位、前缀与代码段错配）→ ``""``，
    调用方 fail-open（不发请求，直接回落 report_list）。
    """
    text = (code or "").strip().upper()
    if not text:
        return ""
    body, _, suffix = text.partition(".")
    prefix = ""
    if len(body) > 6 and body[:2] in _EXCHANGE_PREFIXES:
        prefix, body = body[:2], body[2:]
    if not body.isdigit() or len(body) != 6:
        return ""
    if body.startswith(("4", "8")):
        # 北交所旧代码段 43xxxx / 83xxxx：实测一律返回「股票代码不合法」，不发请求
        return ""
    if suffix:
        if suffix not in _EXCHANGE_PREFIXES:
            return ""
        if prefix and prefix != suffix:
            return ""
        prefix = prefix or suffix
    inferred = _infer_prefix(body)
    if prefix and inferred and prefix != inferred:
        return ""  # 前缀与代码段错配（实测必然被接口拒绝，省掉一次请求）
    prefix = prefix or inferred
    if not prefix:
        return ""
    return f"{prefix}{body}"


# ─────────────────────────────────────────────────────────────────────────────
# 拉取 + 原始响应 → canonical 快照
# ─────────────────────────────────────────────────────────────────────────────
def _block_type_error(payload: dict[str, Any]) -> str:
    """检查 5 个区块的值类型；必需区块非 list → 返回原因（空串 = 通过）。

    ``{"pjtj": [], "jgyc": 5, "yctj_list": 5}`` 这类形态若只判键存在，后续 ``for row in
    payload["yctj_list"]`` 会抛 ``TypeError``（违背「绝不抛」承诺），故在入口判死。
    ``yctj_chart`` / ``ycmx`` 本模块不消费：非 list 只记 warning，不判死。
    """
    for key in _REQUIRED_BLOCKS:
        if not isinstance(payload.get(key), list):
            return f"invalid_block:{key}"
    for key in _UNUSED_BLOCKS:
        value = payload.get(key)
        if value is not None and not isinstance(value, list):
            logger.warning("f10 profit_forecast 区块 %s 非 list（%r），本模块不消费，忽略", key, type(value).__name__)
    return ""


def _fetch_raw(code: str, *, timeout: int) -> tuple[dict[str, Any], str]:
    """拉原始响应，返回 ``(payload, 不可用原因)``；可用时原因为空串。"""
    secucode = market_prefixed_code(code)
    if not secucode:
        return {}, "unsupported_code"
    try:
        response = requests.get(F10_FORECAST_URL, params={"code": secucode}, timeout=timeout)
    except requests.RequestException as exc:  # 网络失败/超时/连接错误
        logger.warning("f10 profit_forecast 请求失败（%s）：%s", secucode, exc)
        return {}, "network_error"
    if response.status_code != 200:
        logger.warning("f10 profit_forecast HTTP %s（%s）", response.status_code, secucode)
        return {}, f"http_{response.status_code}"
    try:
        payload = response.json()
    except ValueError:  # 非 JSON（限流页/空体）
        logger.warning("f10 profit_forecast 响应非 JSON（%s）", secucode)
        return {}, "invalid_json"
    # 非法代码形态：HTTP 200 + {"status": -1, "message": "股票代码不合法"}
    if not isinstance(payload, dict) or payload.get("status") == -1:
        return {}, "invalid_code"
    if not all(key in payload for key in _REQUIRED_BLOCKS):
        return {}, "unexpected_payload"
    block_error = _block_type_error(payload)  # 类型守卫：非 list 区块不得进入迭代
    if block_error:
        logger.warning("f10 profit_forecast 区块类型非法（%s）：%s", secucode, block_error)
        return {}, block_error
    return payload, ""


def _predict_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """``yctj_list`` 中 ``YEAR_MARK == "E"`` 的行，保留接口返回行序（实测 = 年度升序）。"""
    rows = [row for row in payload.get("yctj_list") or [] if isinstance(row, dict)]
    return [row for row in rows if str(row.get("YEAR_MARK") or "").strip().upper() == "E"]


def _snapshot_date(payload: dict[str, Any]) -> date:
    """快照日：聚合行 ``PUBLISH_DATE`` 的日期部分；缺失 → 本地抓取日（冒烟 §9.1）。"""
    rows = [row for row in payload.get("jgyc") or [] if isinstance(row, dict)]
    for row in rows:
        if str(row.get("ORG_CODE") or "").strip() == _AGGREGATE_ORG_CODE:
            parsed = _parse_date(row.get("PUBLISH_DATE"))
            if parsed is not None:
                return parsed
    if rows:
        parsed = _parse_date(rows[0].get("PUBLISH_DATE"))
        if parsed is not None:
            return parsed
    return date.today()


def _to_count(value: Any) -> int | None:
    """家数类字段（``EPS_COUNT`` 等）→ ``int``；缺失/异常 → ``None``。"""
    number = _to_float(value)
    return int(number) if number is not None else None


def _year_of(row: dict[str, Any]) -> int | None:
    number = _to_float(row.get("YEAR"))
    return int(number) if number is not None else None


def canonical_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """原始响应 → 快照 canonical dict（逐字段对应冒烟报告 §8.1，**不含** max/min 与评级）。

    ``eps_fy1/fy2/fy3`` = 第 1/2/3 个 ``YEAR_MARK == "E"`` 行的 ``EPS``（全量家数均值）；
    ``coverage_count`` = 与 ``eps_fy1`` **同一行**的 ``EPS_COUNT``（机构家数，非研报数）。
    """
    rows = _predict_rows(payload)
    fy1 = rows[0] if len(rows) >= 1 else {}
    fy2 = rows[1] if len(rows) >= 2 else {}
    fy3 = rows[2] if len(rows) >= 3 else {}
    return {
        "as_of_date": _snapshot_date(payload).isoformat(),
        "eps_fy1": _to_float(fy1.get("EPS")),
        "eps_fy2": _to_float(fy2.get("EPS")),
        "eps_fy3": _to_float(fy3.get("EPS")),
        "eps_fy1_year": _year_of(fy1),
        "eps_fy2_year": _year_of(fy2),
        "eps_fy3_year": _year_of(fy3),
        "coverage_count": _to_count(fy1.get("EPS_COUNT")),
        # 审计留痕（冒烟 §4.3）：上一期均值与家数。**不参与** revision——设计 §5.2 要求
        # revision 由自建快照序列计算；该列语义（上月快照 vs 上次刷新）尚待两日对拍确认。
        "eps_fy1_lastmonths": _to_float(fy1.get("EPS_LASTMONTHS")),
        "eps_fy1_lastmonths_count": _to_count(fy1.get("EPS_LASTMONTHS_COUNT")),
        "source": SOURCE_NAME,
    }


def _collect(code: str, *, timeout: int) -> tuple[dict[str, Any], str]:
    """拉取 + 映射，返回 ``(快照, 不可用原因)``；可用时原因为空串。"""
    payload, reason = _fetch_raw(code, timeout=timeout)
    if reason:
        return {}, reason
    snapshot = canonical_snapshot(payload)
    if snapshot.get("eps_fy1") is None:
        # 无机构覆盖（jgyc 空且无 E 行）：是合法数据状态，但对共识无用
        return {}, "no_coverage"
    return snapshot, ""


def fetch_profit_forecast(code: str, *, timeout: int = 20) -> dict[str, Any]:
    """拉取单次一致预期快照（canonical dict），fail-open 返回 ``{}``。

    不可用情形一律返回 ``{}``（绝不抛）：网络失败 / 非 JSON / 非法代码（``status == -1``） /
    北交所旧代码段（不发请求）/ 无机构覆盖（``jgyc`` 空且无 ``YEAR_MARK == "E"`` 行）。

    Returns:
        形如 ``{"as_of_date", "eps_fy1", "eps_fy2", "eps_fy3", "coverage_count",
        "eps_fy1_year", "eps_fy2_year", "eps_fy3_year", "eps_fy1_lastmonths",
        "eps_fy1_lastmonths_count", "source"}`` 的快照；不可用 → ``{}``。
    """
    snapshot, _reason = _collect(code, timeout=timeout)
    return snapshot


# ─────────────────────────────────────────────────────────────────────────────
# 快照落盘（append-only + 同日幂等 + 损坏行跳过）
# ─────────────────────────────────────────────────────────────────────────────
def snapshots_dir(data_dir: str | Path | None = None) -> Path:
    """快照目录：显式 ``data_dir`` 优先，否则 ``<data_root>/consensus_snapshots``。"""
    if data_dir is not None:
        return Path(data_dir)
    from alphabee.utils.storage import get_data_root

    return get_data_root() / SNAPSHOT_DIR_NAME


def _snapshot_stem(symbol: str) -> str:
    """快照文件名单一形态：**纯 6 位代码段**（``300750.SZ`` / ``sz300750`` / ``300750`` → ``300750``）。

    evidence 路径传 ``300750.SZ``、factors 路径传 ``300750``，若按原样命名会落两个文件
    （revision 序列重复、审计混乱）——故写入与读取都归一到这里。
    非标准输入（无法提取 6 位代码）→ 退回 ``normalize_symbol`` 的净化形态（防路径穿越）。
    """
    text = (symbol or "").strip().upper()
    body, _sep, _suffix = text.partition(".")
    if len(body) > 6 and body[:2] in _EXCHANGE_PREFIXES:
        body = body[2:]
    if len(body) == 6 and body.isdigit():
        return body
    from alphabee.utils.storage import normalize_symbol

    return normalize_symbol(symbol)


def _snapshot_path(symbol: str, data_dir: str | Path | None = None) -> Path:
    return snapshots_dir(data_dir) / f"{_snapshot_stem(symbol)}.jsonl"


def _open_append(path: Path) -> Any:
    """以追加模式打开快照文件（单点可替换：测试注入 ``OSError`` 验证写盘 fail-open）。"""
    return path.open("a", encoding="utf-8")


def load_snapshots(symbol: str, *, data_dir: str | Path | None = None) -> list[dict[str, Any]]:
    """读回某标的快照序列（按 ``as_of_date`` 升序；同日期保留首条；损坏行跳过）。

    文件缺失 / 目录不可读（``OSError``）→ ``[]``（fail-open，不抛）。以下行被跳过（不抛）：
    非 JSON 行、空行、非 dict 行、缺 ``as_of_date`` 或日期不可解析的行。
    """
    try:
        path = _snapshot_path(symbol, data_dir)
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
    except OSError as exc:  # 目录不可读/权限/竞态 → 当作无历史，由调用方降级
        logger.warning("consensus snapshot 读取失败（%s）：%s", symbol, exc)
        return []
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(row, dict):
            continue
        parsed = _parse_date(row.get("as_of_date"))
        if parsed is None:
            continue
        as_of = parsed.isoformat()
        if as_of in seen:  # 同日重复行（手工修复/并发追加）→ 保留首条
            continue
        seen.add(as_of)
        rows.append({**row, "as_of_date": as_of})
    rows.sort(key=lambda row: str(row["as_of_date"]))
    return rows


def _append_snapshot_row(
    symbol: str,
    values: dict[str, Any],
    *,
    data_dir: str | Path | None,
) -> tuple[str, str]:
    """落盘一行快照，返回 ``(as_of_date, 失败说明)``；成功或同日幂等 → 失败说明为空串。

    纪律（设计 §5.2）：

    - **append-only**：只追加新行，绝不改写/覆盖历史行；
    - **同日幂等**：该标的已有同 ``as_of_date`` 行时不再追加——接口 CDN 缓存 120 秒，
      同日多次抓取只保留第一条，保证 revision 序列一天一个点；
    - 空 ``values`` / 缺 ``as_of_date`` / ``eps_fy1`` 缺失 → ``("", "unusable_values")``
      （无覆盖日的空快照对 revision 无意义，不污染序列）；
    - **写盘 fail-open**：``mkdir`` / ``open`` / ``write`` 的 ``OSError``（只读目录、磁盘满、
      权限）全部捕掉不抛，折算为 ``(as_of, "write_failed: ...")``——调用方（
      :func:`build_f10_consensus`）仍可用内存快照产出当期结果并留痕。
    """
    as_of = _parse_date((values or {}).get("as_of_date"))
    if as_of is None or _to_float((values or {}).get("eps_fy1")) is None:
        return "", "unusable_values"
    as_of_text = as_of.isoformat()
    path = None
    try:
        if any(str(row.get("as_of_date")) == as_of_text for row in load_snapshots(symbol, data_dir=data_dir)):
            return as_of_text, ""  # 同日幂等
        path = _snapshot_path(symbol, data_dir)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {"as_of_date": as_of_text, **{k: v for k, v in values.items() if k != "as_of_date"}}
        with _open_append(path) as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    except OSError as exc:  # 只读目录 / 磁盘满 / 权限：绝不穿透 provider 链
        logger.warning("consensus snapshot 落盘失败（%s）：%s", path or symbol, exc)
        return as_of_text, f"write_failed: {exc}"
    return as_of_text, ""


def append_snapshot(symbol: str, values: dict[str, Any], *, data_dir: str | Path | None = None) -> str:
    """把一次快照追加进 ``<data_dir>/<6位代码>.jsonl``，返回落盘的 ``as_of_date``。

    返回 ``""`` 的两种情形：值不可用（空 ``values`` / 缺 ``as_of_date`` / ``eps_fy1`` 缺失），
    或**写盘失败**（``OSError``，fail-open 不抛，仅 logger 留痕）。同日幂等 → 返回该日期。
    需要区分「写盘失败」时用 :func:`_append_snapshot_row`（返回失败说明）。
    """
    as_of, _error = _append_snapshot_row(symbol, values, data_dir=data_dir)
    return as_of if _error == "" else ""


# ─────────────────────────────────────────────────────────────────────────────
# revision 自算（复用 engine 既有窗口原语，纯规则、可离线重放）
# ─────────────────────────────────────────────────────────────────────────────
def _eps_series(snapshots: list[dict[str, Any]], field: str) -> list[tuple[date, float]]:
    """快照序列 → ``(date, eps)`` 升序序列（缺值/坏日期行跳过）。"""
    series: list[tuple[date, float]] = []
    for row in snapshots:
        parsed = _parse_date(row.get("as_of_date"))
        value = _to_float(row.get(field))
        if parsed is None or value is None:
            continue
        series.append((parsed, value))
    series.sort()
    return series


def _revisions_from_rows(
    snapshots: list[dict[str, Any]], *, as_of: str | date | None = None
) -> dict[str, float | None]:
    """由快照行序列自算 revision（PERCENT，正=上修），不足窗口 → ``None``（不编造）。

    窗口口径与 :mod:`engine` 同构：当期值 = ``as_of`` 当日或之前最近一条快照的
    ``eps_fy1``；基准值 = ``as_of - window_days`` 当日或之前最近一条；变化率 =
    ``(当期 / 基准 - 1) × 100``。``revision_acceleration`` 为 ``revision_1m`` 序列的
    二阶差分（与 engine 同一算法），需 ≥2 个可算点。

    独立于「从磁盘读」的原因：写盘失败时当次快照仍在内存里，必须与磁盘历史合成同一
    序列后计算（否则当期点丢失，revision 与落盘成功时不一致）。
    """
    asof = _parse_date(as_of) or date.today()
    out: dict[str, float | None] = {}
    for source_field, canon, window in _REVISION_WINDOWS:
        series = _eps_series(snapshots, source_field)
        recent = _latest_at_or_before(series, asof)
        past = _latest_at_or_before(series, asof - timedelta(days=window))
        out[canon] = _pct_change(past, recent)
    rev_series = _revision_series(_eps_series(snapshots, "eps_fy1"), 30)
    if len(rev_series) >= 2:
        out["revision_acceleration"] = round(rev_series[-1][1] - rev_series[-2][1], 4)
    else:
        out["revision_acceleration"] = None
    return out


def compute_revisions(
    symbol: str,
    *,
    data_dir: str | Path | None = None,
    as_of: str | date | None = None,
) -> dict[str, float | None]:
    """由**磁盘**快照序列自算 revision（PERCENT，正=上修），不足窗口 → ``None``。

    Args:
        as_of: 计算基准日（缺省 = 今天）；与 ``ConsensusOutput.as_of_date`` 同口径。

    Returns:
        ``{"eps_fy1_revision_1m", "eps_fy1_revision_3m", "eps_fy2_revision_1m",
        "revision_acceleration"}`` → ``float`` 或 ``None``。
    """
    return _revisions_from_rows(load_snapshots(symbol, data_dir=data_dir), as_of=as_of)


def _merge_with_history(history: list[dict[str, Any]], current: dict[str, Any]) -> list[dict[str, Any]]:
    """磁盘历史 + 当次内存快照（同 ``as_of_date`` 以历史行优先，与同日幂等一致）。"""
    as_of = str(current.get("as_of_date") or "")
    rows = list(history)
    if as_of and not any(str(row.get("as_of_date")) == as_of for row in rows):
        rows.append(current)
    return rows


# ─────────────────────────────────────────────────────────────────────────────
# 快照 → ConsensusOutput
# ─────────────────────────────────────────────────────────────────────────────
def build_f10_consensus(
    code: str,
    *,
    timeout: int = 20,
    data_dir: str | Path | None = None,
    as_of_date: str | None = None,
) -> ConsensusOutput | None:
    """F10 快照 → :class:`ConsensusOutput`；不可用（拉取失败 / 无覆盖）→ ``None``。

    可用时副作用：先把本快照 append 进快照序列（同日幂等），再由**含本次**的序列自算
    revision。``as_of_date`` 缺省取快照自带日期（= ``jgyc[0].PUBLISH_DATE``，即数据刷新日；
    冒烟 §9.1 建议口径），revision 事件日与 :class:`ConsensusOutput.as_of_date` 同源，
    沿用 P0-1 口径。

    新源上不可得的字段显式置 ``None`` 并登记 ``consensus_missing``：``target_price``
    （接口无此字段）、``rating_mean`` / ``rating_upgrade_1m`` / ``rating_downgrade_1m`` /
    ``revision_breadth``（量表不可比 / 无上期评级）。

    **写盘 fail-open**：快照落盘失败（只读目录 / 磁盘满 / 权限）不抛、不回落——当次快照
    仍进入 ``values``，revision 用「磁盘历史 + 当次内存快照」计算（与落盘成功时逐位一致），
    失败原因写入 ``ConsensusOutput.warnings``。
    """
    snapshot, reason = _collect(code, timeout=timeout)
    if not snapshot:
        logger.warning("f10 profit_forecast 不可用（%s）：%s", code, reason)
        return None

    asof = _parse_date(as_of_date) or _parse_date(snapshot.get("as_of_date")) or date.today()
    history = load_snapshots(code, data_dir=data_dir)  # 已 fail-open（读失败 → []）
    _as_of_text, write_error = _append_snapshot_row(code, snapshot, data_dir=data_dir)
    warnings: list[str] = []
    if write_error and write_error != "unusable_values":
        warnings.append(f"consensus snapshot 落盘失败（{write_error}），本次仍使用内存快照参与 revision 计算")
    revisions = _revisions_from_rows(_merge_with_history(history, snapshot), as_of=asof)

    values: dict[str, float | int | None] = {field: None for field in CONSENSUS_FIELDS}
    values.update(
        {
            "eps_fy1": _to_float(snapshot.get("eps_fy1")),
            "eps_fy2": _to_float(snapshot.get("eps_fy2")),
            "eps_fy3": _to_float(snapshot.get("eps_fy3")),
            "coverage_count": _to_count(snapshot.get("coverage_count")),
            **revisions,
        }
    )
    return ConsensusOutput(
        values=values,
        source=SOURCE_NAME,
        missing=[f"consensus_missing: {field}" for field in CONSENSUS_FIELDS if values.get(field) is None],
        warnings=warnings,
        as_of_date=asof.isoformat(),
        # 新源不统计研报条数（恒 0）；覆盖机构数见 coverage_count（口径变更见设计 §5.4）
        report_count=0,
    )
